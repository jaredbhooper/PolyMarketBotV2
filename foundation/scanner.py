"""Polymarket market scanner. Strategy-agnostic.

Returns a list of Market objects (strategies/base.py) for all the open
markets across the configured strategies' relevant universes.

The scanner intentionally fetches more broadly than any one strategy needs
- strategies.relevant_markets() narrows it. That keeps the scanner reusable.
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterable

import requests

from strategies.base import ArbEvent, ArbLeg, Market


GAMMA_DEFAULT = "https://gamma-api.polymarket.com"
CLOB_DEFAULT = "https://clob.polymarket.com"


# Patterns inside Polymarket weather group titles:
#   "13C or below" / "13°C or below"
#   "14C" / "14°C"
#   "32C or above" / "32°C or above" / "32C or higher"
#   "78-79F" (two-degree range)
_RANGE_TWO_RE = re.compile(
    r"(?P<lo>-?\d+)\s*-\s*(?P<hi>-?\d+)\s*°?(?P<unit>[CF])?",
    re.IGNORECASE,
)
_RANGE_ONE_RE = re.compile(
    r"(?P<num>-?\d+)\s*°?(?P<unit>[CF])?\s*(?P<qual>or\s+(?:below|above|higher|lower|more|less))?",
    re.IGNORECASE,
)


def parse_group_title(title: str) -> dict | None:
    """Extract threshold info from a Polymarket weather range title.

    Returns dict like:
      "14°C"            -> {threshold:14, unit:'C', bound:'eq', lo:14, hi:14}
      "13°C or below"   -> {threshold:13, unit:'C', bound:'le', lo:None, hi:13}
      "32°C or above"   -> {threshold:32, unit:'C', bound:'ge', lo:32, hi:None}
      "78-79°F"         -> {threshold:78, unit:'F', bound:'range', lo:78, hi:79}
    """
    if not title:
        return None
    t = title.replace("°", "")
    # Two-number range first (more specific)
    m2 = _RANGE_TWO_RE.search(t)
    if m2:
        try:
            lo, hi = int(m2.group("lo")), int(m2.group("hi"))
        except (TypeError, ValueError):
            return None
        if lo > hi:
            lo, hi = hi, lo
        u = (m2.group("unit") or "").upper()
        unit = "F" if u == "F" else "C" if u == "C" else "C"
        return {"threshold": lo, "unit": unit, "bound": "range", "lo": lo, "hi": hi}
    m = _RANGE_ONE_RE.search(t)
    if not m:
        return None
    try:
        n = int(m.group("num"))
    except (TypeError, ValueError):
        return None
    u = (m.group("unit") or "").upper()
    unit = "F" if u == "F" else "C" if u == "C" else "C"
    qual = (m.group("qual") or "").lower()
    if "below" in qual or "lower" in qual or "less" in qual:
        bound = "le"
        lo, hi = None, n
    elif "above" in qual or "higher" in qual or "more" in qual:
        bound = "ge"
        lo, hi = n, None
    else:
        bound = "eq"
        lo, hi = n, n
    return {"threshold": n, "unit": unit, "bound": bound, "lo": lo, "hi": hi}


def _parse_outcomes(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


class Scanner:
    def __init__(self, cfg: dict):
        sc = cfg.get("scanner", {})
        self.gamma = sc.get("gamma_url", GAMMA_DEFAULT).rstrip("/")
        self.clob = sc.get("clob_url", CLOB_DEFAULT).rstrip("/")
        self.timeout = int(sc.get("request_timeout", 30))
        self.page_limit = int(sc.get("page_limit", 500))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolyMarketBotV2/0.1 (paper)"})

    # ------------------------------------------------------------------ HTTP
    def _get(self, url: str, params: dict | None = None, retries: int = 3):
        last_err = None
        for i in range(retries):
            try:
                r = self._session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.RequestException as e:
                last_err = str(e)
            time.sleep(0.5 * (2 ** i))
        raise RuntimeError(f"GET {url} failed after {retries} tries: {last_err}")

    # ------------------------------------------------------------------ events
    def fetch_events(self, tag_slug: str, limit: int | None = None) -> list[dict]:
        """Fetch open, active events under a tag_slug. Paginates via offset."""
        out: list[dict] = []
        offset = 0
        page = min(limit or self.page_limit, self.page_limit)
        while True:
            params = {
                "closed": "false",
                "active": "true",
                "tag_slug": tag_slug,
                "limit": page,
                "offset": offset,
            }
            batch = self._get(f"{self.gamma}/events", params)
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < page or (limit is not None and len(out) >= limit):
                break
            offset += page
        return out[: limit] if limit else out

    # ------------------------------------------------------------------ books
    def fetch_book(self, token_id: str) -> dict | None:
        try:
            return self._get(f"{self.clob}/book", {"token_id": token_id})
        except RuntimeError:
            return None

    @staticmethod
    def _normalize_levels(levels: list[dict] | None, ascending: bool) -> list[dict]:
        if not levels:
            return []
        out = []
        for lvl in levels:
            try:
                p = float(lvl["price"])
                s = float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if s <= 0 or not (0.0 < p < 1.0):
                continue
            out.append({"price": p, "size": s})
        out.sort(key=lambda x: x["price"], reverse=not ascending)
        return out

    # ------------------------------------------------------------------ build
    def build_market(self, event: dict, market: dict,
                     kind: str = "max") -> Market | None:
        cid = market.get("conditionId") or market.get("condition_id")
        if not cid:
            return None
        tokens = _parse_outcomes(market.get("clobTokenIds"))
        outcomes = _parse_outcomes(market.get("outcomes"))
        prices = _parse_outcomes(market.get("outcomePrices"))
        yes_token = tokens[0] if len(tokens) > 0 else None
        no_token = tokens[1] if len(tokens) > 1 else None

        yes_book_asks: list[dict] = []
        yes_book_bids: list[dict] = []
        no_book_asks: list[dict] = []
        no_book_bids: list[dict] = []
        if yes_token:
            yb = self.fetch_book(yes_token) or {}
            # Asks: ascending (best ask = lowest); Bids: descending (best bid = highest).
            yes_book_asks = self._normalize_levels(yb.get("asks"), ascending=True)
            yes_book_bids = self._normalize_levels(yb.get("bids"), ascending=False)
        if no_token:
            nb = self.fetch_book(no_token) or {}
            no_book_asks = self._normalize_levels(nb.get("asks"), ascending=True)
            no_book_bids = self._normalize_levels(nb.get("bids"), ascending=False)

        yes_ask = yes_book_asks[0]["price"] if yes_book_asks else None
        yes_bid = yes_book_bids[0]["price"] if yes_book_bids else None
        no_ask = no_book_asks[0]["price"] if no_book_asks else None
        no_bid = no_book_bids[0]["price"] if no_book_bids else None
        if yes_ask is None and len(prices) >= 1:
            try:
                yes_ask = float(prices[0])
            except (TypeError, ValueError):
                pass

        book_depth_usd = sum(l["price"] * l["size"] for l in yes_book_asks)

        # Strategy hints parsed out of the slug / group title.
        gt = market.get("groupItemTitle") or ""
        parsed = parse_group_title(gt) or {}

        rules = (
            market.get("description")
            or event.get("description")
            or ""
        )
        # Pull station info out of the description for downstream grading.
        station_url = None
        url_match = re.search(r"https?://www\.wunderground\.com/history/\S+", rules)
        if url_match:
            station_url = url_match.group(0).rstrip(".,)")

        resolution_source = event.get("resolutionSource") or station_url or "wunderground"

        end_iso = market.get("endDateIso") or event.get("eventDate")

        return Market(
            market_id=cid,
            slug=market.get("slug") or event.get("slug") or "",
            question=market.get("question") or event.get("title") or "",
            category=event.get("seriesSlug") or "highest-temperature",
            rules_text=rules,
            resolve_date=end_iso,
            end_date_iso=market.get("endDate") or event.get("endDate"),
            yes_token_id=str(yes_token) if yes_token else None,
            no_token_id=str(no_token) if no_token else None,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            yes_book=yes_book_asks,
            no_book=no_book_asks,
            yes_book_bids=yes_book_bids,
            no_book_bids=no_book_bids,
            book_depth_usd=book_depth_usd,
            extras={
                "event_slug": event.get("slug"),
                "event_title": event.get("title"),
                "event_id": event.get("id"),
                "group_item_title": gt,
                "parsed_threshold": parsed.get("threshold"),
                "parsed_unit": parsed.get("unit"),
                "parsed_bound": parsed.get("bound"),
                "lo": parsed.get("lo"),
                "hi": parsed.get("hi"),
                "station_url": station_url,
                "kind": kind,                       # 'max' = highest-temp, 'min' = lowest-temp
                "outcomes": outcomes,
                "outcome_prices": prices,
                "volume_24h": market.get("volume24hr"),
                "liquidity": market.get("liquidity"),
            },
        )

    def scan_tag(self, tag_slug: str, limit: int | None = None,
                 fetch_books: bool = True) -> tuple[list[dict], list[Market]]:
        """Returns (raw events, built Market objects with books).

        If fetch_books is False, books are skipped (faster, for early debugging).

        `kind` is derived from the tag_slug: highest-temperature -> 'max',
        lowest-temperature -> 'min', otherwise 'max' (default).
        """
        events = self.fetch_events(tag_slug=tag_slug, limit=limit)
        kind = "min" if "lowest" in tag_slug else "max"
        out: list[Market] = []
        for e in events:
            for m in e.get("markets") or []:
                if m.get("closed") or not m.get("active"):
                    continue
                if fetch_books:
                    market = self.build_market(e, m, kind=kind)
                else:
                    # Skip per-token book lookup; use gamma fields only.
                    tokens = _parse_outcomes(m.get("clobTokenIds"))
                    market = Market(
                        market_id=m.get("conditionId", ""),
                        slug=m.get("slug", ""),
                        question=m.get("question", ""),
                        category=tag_slug,
                        rules_text=m.get("description", ""),
                        resolve_date=m.get("endDateIso"),
                        end_date_iso=m.get("endDate"),
                        yes_token_id=str(tokens[0]) if tokens else None,
                        no_token_id=str(tokens[1]) if len(tokens) > 1 else None,
                        yes_ask=m.get("bestAsk"),
                        yes_bid=m.get("bestBid"),
                        no_ask=None, no_bid=None,
                        extras={
                            "event_slug": e.get("slug"),
                            "event_title": e.get("title"),
                            "group_item_title": m.get("groupItemTitle"),
                            "kind": kind,
                            **(parse_group_title(m.get("groupItemTitle", "")) or {}),
                        },
                    )
                if market:
                    out.append(market)
        return events, out


    # ============================================================== events
    # Event-level (multi-outcome) scanning for bucket-sum arb. Returns one
    # ArbEvent per Polymarket event, MECE-flag (negRisk) and per-leg books
    # included only when needed.
    #
    # Two-stage to keep CLOB cost bounded:
    #   1. Cheap pass: pull every Gamma event, compute sum(bestAsk) and
    #      sum(1-bestBid). Mark each event as 'gamma_only' if neither sum
    #      is within `walk_band` of crossing the arb threshold.
    #   2. Expensive pass: for events where the cheap pass says we might
    #      cross, walk every leg's YES + NO order book on the CLOB.
    #
    # The caller decides which stage to run via `fetch_books`.

    def fetch_all_events(self) -> list[dict]:
        """Paginate every open+active event on Gamma. Returns the raw
        event dicts; caller filters / groups."""
        out: list[dict] = []
        seen: set = set()
        offset = 0
        page = 100   # gamma caps at 100 even when you ask for more
        while True:
            batch = self._get(f"{self.gamma}/events", params={
                "closed": "false",
                "active": "true",
                "limit": page,
                "offset": offset,
            })
            if not isinstance(batch, list) or not batch:
                break
            new = [e for e in batch if e.get("id") not in seen]
            if not new:
                break
            for e in new:
                seen.add(e["id"])
            out.extend(new)
            if len(batch) < page:
                break
            offset += len(batch)
            if offset > 50000:
                break
        return out

    @staticmethod
    def _verify_completeness(event: dict) -> tuple[bool, str]:
        """A market set is MECE iff Polymarket flagged the event negRisk=True
        and every leg is deployed (clobTokenIds present), active, open.
        Polymarket's negRisk flag is the gold-standard MECE assertion -
        without it we cannot confirm completeness from the API alone."""
        if not event.get("negRisk"):
            return False, "event.negRisk != True (not a MECE set)"
        markets = event.get("markets") or []
        if len(markets) < 2:
            return False, f"only {len(markets)} legs"
        deployed = 0
        for m in markets:
            if not m.get("clobTokenIds"):
                return False, f"leg {m.get('groupItemTitle') or m.get('id')} not deployed"
            if m.get("closed") or not m.get("active"):
                return False, f"leg {m.get('groupItemTitle')} closed/inactive"
            deployed += 1
        return True, f"negRisk + {deployed} deployed legs"

    def build_arb_event(self, event: dict, fetch_books: bool = False) -> ArbEvent:
        """Group event into ArbEvent. If fetch_books, pulls per-leg YES+NO
        CLOB books; otherwise only the Gamma snapshot bestAsk/Bid populates
        each leg (no book walk possible)."""
        ok, note = self._verify_completeness(event)
        legs: list[ArbLeg] = []
        for m in event.get("markets") or []:
            tokens = _parse_outcomes(m.get("clobTokenIds"))
            yes_tok = str(tokens[0]) if len(tokens) > 0 else None
            no_tok = str(tokens[1]) if len(tokens) > 1 else None
            try:
                g_yes_ask = float(m["bestAsk"]) if m.get("bestAsk") is not None else None
            except (TypeError, ValueError):
                g_yes_ask = None
            try:
                g_yes_bid = float(m["bestBid"]) if m.get("bestBid") is not None else None
            except (TypeError, ValueError):
                g_yes_bid = None
            leg = ArbLeg(
                market_id=m.get("conditionId") or "",
                leg_title=m.get("groupItemTitle") or m.get("question") or "",
                yes_token_id=yes_tok,
                no_token_id=no_tok,
                gamma_yes_ask=g_yes_ask,
                gamma_yes_bid=g_yes_bid,
                end_date_iso=m.get("endDate") or m.get("endDateIso"),
                extras={
                    "slug": m.get("slug"),
                    "question": m.get("question"),
                    "liquidity": m.get("liquidity"),
                    "volume_24h": m.get("volume24hr"),
                },
            )
            if fetch_books and yes_tok:
                yb = self.fetch_book(yes_tok) or {}
                leg.yes_asks = self._normalize_levels(yb.get("asks"), ascending=True)
                leg.yes_bids = self._normalize_levels(yb.get("bids"), ascending=False)
            if fetch_books and no_tok:
                nb = self.fetch_book(no_tok) or {}
                leg.no_asks = self._normalize_levels(nb.get("asks"), ascending=True)
                leg.no_bids = self._normalize_levels(nb.get("bids"), ascending=False)
            legs.append(leg)
        return ArbEvent(
            event_id=str(event.get("id") or ""),
            event_slug=event.get("slug") or "",
            event_title=event.get("title") or "",
            end_date_iso=event.get("endDate"),
            neg_risk=bool(event.get("negRisk")),
            legs=legs,
            completeness_verified=ok,
            completeness_note=note,
            books_fetched=fetch_books,
            extras={
                "tags": [t.get("slug") for t in (event.get("tags") or []) if isinstance(t, dict)],
                "ticker": event.get("ticker"),
            },
        )


def render_scanner_table(markets: Iterable[Market], max_rows: int = 50) -> str:
    rows = []
    rows.append(
        "| event slug                                       | range          "
        "| YES ask | YES bid | NO ask  | depth $ | resolves   |"
    )
    rows.append(
        "|--------------------------------------------------|----------------"
        "|---------|---------|---------|---------|------------|"
    )
    count = 0
    for m in markets:
        if count >= max_rows:
            break
        slug = (m.extras.get("event_slug") or m.slug)[:48]
        rng = (m.extras.get("group_item_title") or "")[:14]
        ya = f"{m.yes_ask:.3f}" if m.yes_ask is not None else "  -  "
        yb = f"{m.yes_bid:.3f}" if m.yes_bid is not None else "  -  "
        na = f"{m.no_ask:.3f}" if m.no_ask is not None else "  -  "
        depth = f"{m.book_depth_usd:7.0f}"
        rd = m.resolve_date or "-"
        rows.append(
            f"| {slug:48s} | {rng:14s} | {ya:>7s} | {yb:>7s} | {na:>7s} "
            f"| {depth:>7s} | {rd:10s} |"
        )
        count += 1
    return "\n".join(rows)
