"""Thin client for the public Polymarket data API.

Read-only, unauthenticated. Used by the copy-trading strategy to:
  - Discover candidate wallets via the global /trades tape.
  - Pull a single wallet's trade / activity / position history.

Polite throttling: exponential backoff on 429/5xx; per-request sleep
configurable. See docs/api_notes.md for the response shapes.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

import requests


DEFAULT_BASE = "https://data-api.polymarket.com"


class PolymarketData:
    def __init__(self, base_url: str = DEFAULT_BASE, timeout: int = 20,
                 per_request_sleep: float = 0.05):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.sleep = per_request_sleep
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolyMarketBotV2/0.4 (paper)"})

    def _get(self, path: str, params: dict | None = None, retries: int = 4):
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

    # ------------------------------------------------------------ /trades
    def fetch_trades(self, user: str | None = None, limit: int = 100,
                      offset: int = 0) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = user
        data = self._get("/trades", params)
        return data if isinstance(data, list) else []

    def iter_trades(self, user: str | None = None, page: int = 100,
                     max_pages: int = 50,
                     stop_before_ts: int | None = None) -> Iterable[dict]:
        """Paginate /trades. Optionally short-circuit when an item's
        timestamp is at or before `stop_before_ts` (used by the
        follower's cursor advance)."""
        offset = 0
        seen = 0
        for _ in range(max_pages):
            batch = self.fetch_trades(user=user, limit=page, offset=offset)
            if not batch:
                break
            for item in batch:
                if stop_before_ts is not None \
                        and int(item.get("timestamp") or 0) <= stop_before_ts:
                    return
                yield item
                seen += 1
            if len(batch) < page:
                break
            offset += len(batch)

    # ------------------------------------------------------------ /positions
    def fetch_positions(self, user: str) -> list[dict]:
        data = self._get("/positions", {"user": user})
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------ /activity
    def fetch_activity(self, user: str, limit: int = 100,
                        offset: int = 0) -> list[dict]:
        data = self._get("/activity",
                         {"user": user, "limit": limit, "offset": offset})
        return data if isinstance(data, list) else []
