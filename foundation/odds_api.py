"""The Odds API client with strict request-budget enforcement.

Reads ODDS_API_KEY from env (or .env). Persistent monthly counter in
SQLite so multiple cycles don't blow through the free tier (450
requests/month default). 30-minute TTL response cache.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader. Tolerates missing files."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except OSError:
        pass


class OddsAPI:
    def __init__(self, ledger, monthly_cap: int = 450,
                  cache_ttl_seconds: int = 1800,
                  base: str = ODDS_API_BASE):
        self.ledger = ledger
        self.monthly_cap = int(monthly_cap)
        self.ttl = int(cache_ttl_seconds)
        self.base = base.rstrip("/")
        load_dotenv()
        self.api_key = os.environ.get("ODDS_API_KEY") or ""
        self.observe_mode = not self.api_key
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolyMarketBotV1/0.5 (paper)"})

    def budget_status(self) -> dict[str, Any]:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        used = self.ledger.odds_api_requests_this_month(month)
        return {
            "month": month, "used": used, "cap": self.monthly_cap,
            "remaining": max(0, self.monthly_cap - used),
            "observe_mode": self.observe_mode,
        }

    def _can_request(self) -> bool:
        if self.observe_mode:
            return False
        s = self.budget_status()
        return s["remaining"] > 0

    def fetch_odds(self, sport: str, regions: str = "us",
                    markets: str = "h2h") -> list[dict]:
        """Fetch bookmaker odds for a sport. Cache hits don't count
        toward the monthly budget. observe_mode returns []."""
        cached = self.ledger.get_odds_cache(sport, ttl_seconds=self.ttl)
        if cached is not None:
            return cached
        if not self._can_request():
            return []
        params = {"apiKey": self.api_key, "regions": regions, "markets": markets}
        try:
            r = self._session.get(f"{self.base}/sports/{sport}/odds",
                                    params=params, timeout=20)
        except requests.RequestException:
            return []
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        self.ledger.record_odds_api_request(month, sport,
                                              status_code=r.status_code)
        if r.status_code != 200:
            return []
        data = r.json()
        self.ledger.put_odds_cache(sport, data)
        return data if isinstance(data, list) else []
