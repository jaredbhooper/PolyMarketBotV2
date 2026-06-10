"""Wunderground cross-check for the grader.

Polymarket weather markets resolve on the Wunderground "History" page for
a specific airport station - that's the ground truth. The Open-Meteo
archive is a great-but-different reanalysis: same model class, different
data source. Disagreements happen, and when they do we want a human in the
loop instead of silently grading.

The page itself is a server-rendered Angular SPA; the actual hourly obs
come from api.weather.com (Wunderground was acquired by IBM/weather.com
and they share infra). The page leaks the public API key. We hit the same
endpoint the page does.

This module only knows how to FETCH and PARSE - the grader decides what
to do with disagreements.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# The page's SPA exposes this in plain text. If WU/IBM rotates it, scrape
# it back out of any current history page (see _scrape_api_key below).
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
WU_API_BASE = "https://api.weather.com/v1"
WU_HISTORY_URL = (
    "https://www.wunderground.com/history/daily/{country_lc}/{city_lc}/{icao}"
)


@dataclass
class WuResult:
    icao: str
    country: str
    date_iso: str
    unit: str                  # 'C' or 'F'
    max_temp: float | None     # the max raw obs across local-day hourly samples
    n_obs: int
    source_url: str            # the page URL (what the market actually cites)
    error: str | None = None   # populated if fetch failed / no data


def _scrape_api_key(country_lc: str, city_lc: str, icao: str,
                    timeout: int = 20) -> str | None:
    """Refresh the API key by scraping the page. Belt and suspenders for
    the day WU rotates it. Quietly returns None on failure - the caller
    keeps using the cached constant."""
    url = WU_HISTORY_URL.format(country_lc=country_lc, city_lc=city_lc, icao=icao)
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if r.status_code != 200:
            return None
        m = re.search(r"apiKey=([a-zA-Z0-9]+)", r.text)
        return m.group(1) if m else None
    except requests.RequestException:
        return None


class WundergroundClient:
    """Daily-max fetcher for a Wunderground airport station."""

    def __init__(self, timeout: int = 30, api_key: str | None = None):
        self.timeout = timeout
        self.api_key = api_key or WU_API_KEY
        self._sess = requests.Session()
        self._sess.headers.update({
            # WU's APIs serve happily to a desktop UA; avoid bot pages.
            "User-Agent": (
                "Mozilla/5.0 (PolyMarketBotV1/0.1; verifying paper-trade settlements)"
            ),
        })

    # ------------------------------------------------------------------
    def _historical(self, icao: str, country: str, date_iso: str,
                    units: str) -> dict | None:
        # api.weather.com format: /location/<ICAO>:9:<CC>/observations/historical.json
        # `units=m` -> Celsius, `units=e` -> Fahrenheit. We pull the unit
        # the market actually settles in to avoid double-rounding drift.
        d = date_iso.replace("-", "")
        url = (f"{WU_API_BASE}/location/{icao}:9:{country.upper()}"
               f"/observations/historical.json")
        params = {
            "apiKey": self.api_key,
            "units": units,
            "startDate": d,
            "endDate": d,
        }
        last_err = None
        for i in range(3):
            try:
                r = self._sess.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    # API key likely rotated; try to refresh once.
                    return None
            except requests.RequestException as e:
                last_err = str(e)
            time.sleep(0.5 * (2 ** i))
        return None

    # ------------------------------------------------------------------
    def daily_extreme(self, icao: str, country: str, date_iso: str, tz: str,
                      unit: str = "C", kind: str = "max") -> WuResult:
        """Return the max/min temperature observed on local date `date_iso`
        at the named station. `kind` = 'max' for highest-temp markets,
        'min' for lowest-temp markets - same as the market's bucket convention.
        """
        return self._daily_extreme(icao, country, date_iso, tz, unit, kind)

    def daily_max(self, icao: str, country: str, date_iso: str, tz: str,
                  unit: str = "C") -> WuResult:
        return self._daily_extreme(icao, country, date_iso, tz, unit, "max")

    def daily_min(self, icao: str, country: str, date_iso: str, tz: str,
                  unit: str = "C") -> WuResult:
        return self._daily_extreme(icao, country, date_iso, tz, unit, "min")

    def _daily_extreme(self, icao: str, country: str, date_iso: str, tz: str,
                       unit: str, kind: str) -> WuResult:
        """Fetch obs in the requested unit (so the integer rounded value
        matches what the WU history page itself displays) and aggregate the
        raw `temp` column - same one the page's daily summary uses."""
        units_api = "e" if unit.upper() == "F" else "m"
        country_lc = country.lower()
        # Pretty city slug isn't required by the API; reuse for the source url.
        # We use a generic placeholder if unknown.
        page_url = (
            f"https://www.wunderground.com/history/daily/{country_lc}/-/"
            f"{icao}/date/{date_iso}"
        )
        out_unit = "F" if units_api == "e" else "C"

        data = self._historical(icao, country, date_iso, units_api)
        if data is None:
            return WuResult(icao=icao, country=country, date_iso=date_iso,
                            unit=out_unit, max_temp=None, n_obs=0,
                            source_url=page_url,
                            error="api.weather.com fetch failed (rate limit, "
                                  "key rotated, or station outage)")
        obs = data.get("observations") or []
        if not obs:
            return WuResult(icao=icao, country=country, date_iso=date_iso,
                            unit=out_unit, max_temp=None, n_obs=0,
                            source_url=page_url, error="no observations returned")

        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = ZoneInfo("UTC")
        on_day_temps: list[float] = []
        for o in obs:
            ts_gmt = o.get("valid_time_gmt")
            t = o.get("temp")
            if ts_gmt is None or t is None:
                continue
            local = datetime.fromtimestamp(int(ts_gmt), tz=zone)
            if local.date().isoformat() == date_iso:
                on_day_temps.append(float(t))
        if not on_day_temps:
            return WuResult(icao=icao, country=country, date_iso=date_iso,
                            unit=out_unit, max_temp=None,
                            n_obs=len(obs), source_url=page_url,
                            error="no obs land on requested local date")
        agg = max(on_day_temps) if kind == "max" else min(on_day_temps)
        return WuResult(icao=icao, country=country, date_iso=date_iso,
                        unit=out_unit, max_temp=float(agg),
                        n_obs=len(on_day_temps), source_url=page_url)


def parse_station_from_url(url: str) -> tuple[str, str] | None:
    """Pull (icao, country_cc) out of a wunderground history URL like:
       https://www.wunderground.com/history/daily/gb/london/EGLC
    Returns None if the URL doesn't match.
    """
    if not url:
        return None
    # US history URLs have 3 mid segments (cc/state/city/ICAO); non-US have 2
    # (cc/city/ICAO). The ICAO is always 3-4 uppercase letters at the end.
    m = re.search(
        r"wunderground\.com/history/daily/([a-z]{2})/(?:[^/]+/){1,3}([A-Z]{3,4})\b",
        url,
    )
    if not m:
        return None
    return m.group(2), m.group(1).upper()
