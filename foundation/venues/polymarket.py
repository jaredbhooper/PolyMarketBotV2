"""Polymarket venue adapter. Wraps the existing Scanner so the
cross-venue strategy can pull VenueMarkets without duplicating any of
the Gamma/CLOB code.

The adapter is intentionally narrow: it only walks the Polymarket event
universe that the cross-venue strategy can plausibly match against
Kalshi (daily weather + a few other categories). Use a tag_slug filter
to keep CLOB cost bounded.
"""
from __future__ import annotations

import re
from typing import Iterable

from foundation.scanner import Scanner
from foundation.venues.base import Venue, VenueMarket


# Cities for which both Polymarket (weather) and Kalshi (daily temperature)
# have parallel markets. Polymarket's resolution station -> resolution-source
# note we record on the VenueMarket. The list is what V1's weather config
# already audited - the cross-venue strategy reuses it.
_POLYMARKET_DAILY_WEATHER_TAGS = ["highest-temperature", "lowest-temperature"]


class PolymarketVenue(Venue):
    name = "polymarket"

    def __init__(self, scanner: Scanner, weather_cities_cfg: list[dict] | None = None):
        self.scanner = scanner
        # Map slug-hint -> resolution-source note. Built from V1's
        # weather config so cross-venue inherits the audited URL.
        self.city_to_source: dict[str, str] = {}
        for c in (weather_cities_cfg or []):
            note = c.get("station_name") or ""
            url = c.get("resolution_source") or ""
            for hint in (c.get("slug_hints") or []) + [c.get("city") or ""]:
                if not hint:
                    continue
                self.city_to_source[hint.lower()] = f"Wunderground {note} ({url})"

    def _source_note(self, slug: str) -> tuple[str, str | None]:
        """Best-effort settlement-source note for a Polymarket market.
        Falls back to a generic "Wunderground (per market description)"
        when we can't identify the city."""
        s = (slug or "").lower()
        for hint, note in self.city_to_source.items():
            if hint in s:
                url = None
                if "(" in note and note.endswith(")"):
                    url = note[note.rindex("(") + 1: -1]
                return note, url
        return "Wunderground (resolution source per market description)", None

    def fetch_markets(self, category_hint: str | None = None,
                       fetch_books: bool = False) -> list[VenueMarket]:
        """Pull every market under the daily-weather tags. By default
        skips per-token CLOB book lookups - the matcher only needs
        slug/title/leg/close to bucket candidates. Books are fetched
        lazily by `fetch_book_for` after classification, so we don't
        pay CLOB cost on thousands of NON-MATCH markets."""
        tags: Iterable[str] = _POLYMARKET_DAILY_WEATHER_TAGS
        seen_ids: set[str] = set()
        out: list[VenueMarket] = []
        for tag in tags:
            _, markets = self.scanner.scan_tag(tag, fetch_books=fetch_books)
            for m in markets:
                if m.market_id in seen_ids:
                    continue
                seen_ids.add(m.market_id)
                source_note, source_url = self._source_note(m.slug or m.extras.get("event_slug") or "")
                out.append(VenueMarket(
                    venue=self.name,
                    venue_market_id=m.market_id,
                    venue_event_id=str(m.extras.get("event_id") or m.extras.get("event_slug") or ""),
                    title=m.question or "",
                    leg_title=m.extras.get("group_item_title") or "",
                    rules_text=m.rules_text or "",
                    settlement_source=source_note,
                    settlement_source_url=source_url or m.extras.get("station_url"),
                    close_time_iso=m.end_date_iso or m.resolve_date,
                    yes_asks=list(m.yes_book or []),
                    no_asks=list(m.no_book or []),
                    yes_bid=m.yes_bid,
                    no_bid=m.no_bid,
                    fee_model={"type": "flat", "per_contract": 0.0},
                    extras={
                        "event_slug": m.extras.get("event_slug"),
                        "event_title": m.extras.get("event_title"),
                        "kind": m.extras.get("kind"),                # 'max' / 'min'
                        "parsed_threshold": m.extras.get("parsed_threshold"),
                        "parsed_unit": m.extras.get("parsed_unit"),
                        "parsed_bound": m.extras.get("parsed_bound"),
                        "parsed_lo": m.extras.get("lo"),
                        "parsed_hi": m.extras.get("hi"),
                        "yes_token_id": m.yes_token_id,
                        "no_token_id": m.no_token_id,
                    },
                ))
        return out

    def fetch_book_for(self, vm: VenueMarket) -> None:
        """Lazy-fetch YES + NO order books for a single VenueMarket. Used
        by the cross-venue strategy after classification to avoid eating
        CLOB cost on thousands of NON-MATCH markets."""
        yes_tok = vm.extras.get("yes_token_id")
        no_tok = vm.extras.get("no_token_id")
        if yes_tok and not vm.yes_asks:
            yb = self.scanner.fetch_book(yes_tok) or {}
            vm.yes_asks = self.scanner._normalize_levels(yb.get("asks"), ascending=True)
            yb_bids = self.scanner._normalize_levels(yb.get("bids"), ascending=False)
            if yb_bids:
                vm.yes_bid = yb_bids[0]["price"]
        if no_tok and not vm.no_asks:
            nb = self.scanner.fetch_book(no_tok) or {}
            vm.no_asks = self.scanner._normalize_levels(nb.get("asks"), ascending=True)
            nb_bids = self.scanner._normalize_levels(nb.get("bids"), ascending=False)
            if nb_bids:
                vm.no_bid = nb_bids[0]["price"]
