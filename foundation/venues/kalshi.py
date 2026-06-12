"""Kalshi venue adapter. Public market-data endpoints only; no auth.

Kalshi convention decoded by inspection (2026-06-10):

  GET /trade-api/v2/markets/{ticker}/orderbook returns:
    {"orderbook_fp": {"yes_dollars": [[price, size], ...],
                       "no_dollars":  [[price, size], ...]}}

  Both arrays are sorted ascending by price. They are **bid books on
  each side** - "yes_dollars" lists buy orders on YES, "no_dollars" lists
  buy orders on NO. The top of YES asks is therefore (1 - max no_dollars
  price), with size equal to the matching NO bid size. We normalize that
  into the venue-neutral `yes_asks` / `no_asks` (ascending price) the
  arbitrage book-walker expects.

Fees: Kalshi's standard quadratic fee on most series is

  fee_per_contract = ceil( fee_multiplier * 7c * price * (1 - price) )

Stored on each VenueMarket so the cross-venue strategy can subtract it
from the locked profit before deciding to trade.
"""
from __future__ import annotations

import math
import time
from typing import Iterable

import requests

from foundation.venues.base import Venue, VenueMarket


KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiVenue(Venue):
    name = "kalshi"

    def __init__(self, base_url: str = KALSHI_API,
                 categories: list[str] | None = None,
                 per_request_sleep: float = 0.0,
                 timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.sleep = per_request_sleep
        # Categories the cross-venue strategy targets. Default is the set
        # most likely to overlap with Polymarket.
        self.categories = categories or [
            "Climate and Weather", "Politics", "Elections",
            "Sports", "Economics", "Financials",
        ]
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolyMarketBotV2/0.2 (paper)"})
        # Lazy-loaded series metadata - the settlement source lives here
        # (it is NOT on the market itself, only on the series).
        self._series_cache: dict[str, dict] = {}

    # ------------------------------------------------------------ http
    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        url = f"{self.base}{path}"
        last = None
        for i in range(retries):
            try:
                r = self._session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    if self.sleep:
                        time.sleep(self.sleep)
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.RequestException as e:
                last = str(e)
            time.sleep(0.5 * (2 ** i))
        raise RuntimeError(f"GET {url} failed: {last}")

    # ------------------------------------------------------------ series
    def load_series(self, ticker: str) -> dict:
        """Series metadata = source-of-truth for settlement source + fee."""
        if ticker in self._series_cache:
            return self._series_cache[ticker]
        try:
            data = self._get(f"/series/{ticker}")
        except RuntimeError:
            data = {}
        s = data.get("series") if isinstance(data, dict) else None
        s = s or {}
        self._series_cache[ticker] = s
        return s

    def load_series_index(self, category: str) -> list[dict]:
        try:
            data = self._get("/series", {"category": category})
        except RuntimeError:
            return []
        series = data.get("series") or []
        for s in series:
            self._series_cache[s["ticker"]] = s
        return series

    # ------------------------------------------------------------ events
    def fetch_events_for_series(self, series_ticker: str,
                                  status: str = "open") -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(50):
            params = {"series_ticker": series_ticker, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get("/events", params)
            except RuntimeError:
                break
            batch = data.get("events") or []
            out.extend(batch)
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
        return out

    def fetch_markets_for_event(self, event_ticker: str) -> list[dict]:
        try:
            data = self._get("/markets", {"event_ticker": event_ticker, "limit": 200})
        except RuntimeError:
            return []
        return data.get("markets") or []

    def fetch_orderbook(self, ticker: str) -> dict | None:
        try:
            data = self._get(f"/markets/{ticker}/orderbook")
        except RuntimeError:
            return None
        return data.get("orderbook_fp") or data.get("orderbook") or {}

    # ------------------------------------------------------------ normalize
    @staticmethod
    def _to_asks_from_opposite_bids(opp_bids: list[list]) -> list[dict]:
        """Convert a list of [price, size] bids on the OPPOSITE side into
        the venue-neutral ascending asks on THIS side.

        A bid of size S at price p on the NO side is identical to an ask
        of size S at price (1 - p) on the YES side. Mirror that mapping
        and re-sort ascending."""
        out: list[dict] = []
        for lvl in opp_bids or []:
            try:
                price = float(lvl[0])
                size = float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            if size <= 0:
                continue
            ask_price = round(1.0 - price, 4)
            if not (0.0 < ask_price < 1.0):
                continue
            out.append({"price": ask_price, "size": size})
        out.sort(key=lambda x: x["price"])
        return out

    @staticmethod
    def _kalshi_fee(price: float, multiplier: float = 1.0) -> float:
        """Thin wrapper around foundation.fees.kalshi_fee_per_contract -
        kept for legacy callers (strategies.cross_venue_arb references
        this static method by name)."""
        from foundation.fees import kalshi_fee_per_contract
        return kalshi_fee_per_contract(price, multiplier=multiplier)

    # ------------------------------------------------------------ public
    def fetch_markets(self, category_hint: str | None = None,
                       verbose: bool = False,
                       fetch_books: bool = False) -> list[VenueMarket]:
        """Pull every open market in the target categories. By default
        skips per-ticker orderbook fetch - the matcher only needs
        title/leg/close/source to bucket candidates. Books are fetched
        lazily by `fetch_book_for` after classification."""
        out: list[VenueMarket] = []
        target_categories = [category_hint] if category_hint else self.categories
        for cat in target_categories:
            series = self.load_series_index(cat)
            if verbose:
                print(f"    kalshi: {cat}: {len(series)} series")
            for ser in series:
                if ser.get("frequency") != "daily" and cat == "Climate and Weather":
                    # First-cut focus: daily weather only on Kalshi, since
                    # that's where the cross-venue overlap actually lives.
                    continue
                ser_ticker = ser["ticker"]
                src = (ser.get("settlement_sources") or [{}])
                src_name = (src[0] if src else {}).get("name", "Kalshi (per series)")
                src_url = (src[0] if src else {}).get("url")
                fee_mult = float(ser.get("fee_multiplier") or 1.0)
                fee_type = ser.get("fee_type") or "quadratic"
                events = self.fetch_events_for_series(ser_ticker)
                for ev in events:
                    et = ev["event_ticker"]
                    markets = self.fetch_markets_for_event(et)
                    for m in markets:
                        if m.get("status") != "active":
                            # Some markets are 'initialized' / 'inactive'.
                            pass
                        if fetch_books:
                            ob = self.fetch_orderbook(m["ticker"]) or {}
                            yes_asks = self._to_asks_from_opposite_bids(
                                ob.get("no_dollars") or [])
                            no_asks = self._to_asks_from_opposite_bids(
                                ob.get("yes_dollars") or [])
                        else:
                            yes_asks = []
                            no_asks = []
                        try:
                            yes_bid_top = float(m.get("yes_bid_dollars")) if m.get("yes_bid_dollars") else None
                        except (TypeError, ValueError):
                            yes_bid_top = None
                        try:
                            no_bid_top = float(m.get("no_bid_dollars")) if m.get("no_bid_dollars") else None
                        except (TypeError, ValueError):
                            no_bid_top = None
                        out.append(VenueMarket(
                            venue=self.name,
                            venue_market_id=m["ticker"],
                            venue_event_id=et,
                            title=ev.get("title") or m.get("title") or "",
                            leg_title=m.get("yes_sub_title") or m.get("title") or "",
                            rules_text=(m.get("rules_primary") or "") + "\n" + (m.get("rules_secondary") or ""),
                            settlement_source=f"{src_name} (Kalshi)",
                            settlement_source_url=src_url,
                            close_time_iso=m.get("close_time") or m.get("expiration_time"),
                            yes_asks=yes_asks,
                            no_asks=no_asks,
                            yes_bid=yes_bid_top,
                            no_bid=no_bid_top,
                            fee_model={"type": fee_type, "multiplier": fee_mult},
                            extras={
                                "series_ticker": ser_ticker,
                                "series_title": ser.get("title"),
                                "series_category": ser.get("category"),
                                "event_title": ev.get("title"),
                                "event_sub_title": ev.get("sub_title"),
                                "yes_sub_title": m.get("yes_sub_title"),
                                "no_sub_title": m.get("no_sub_title"),
                                "strike_type": m.get("strike_type"),
                                "expiration_time": m.get("expiration_time"),
                            },
                        ))
        return out

    def fetch_book_for(self, vm: VenueMarket) -> None:
        """Lazy-fetch + normalize the orderbook for a single VenueMarket."""
        if vm.yes_asks and vm.no_asks:
            return
        ob = self.fetch_orderbook(vm.venue_market_id) or {}
        if not vm.yes_asks:
            vm.yes_asks = self._to_asks_from_opposite_bids(
                ob.get("no_dollars") or [])
        if not vm.no_asks:
            vm.no_asks = self._to_asks_from_opposite_bids(
                ob.get("yes_dollars") or [])
