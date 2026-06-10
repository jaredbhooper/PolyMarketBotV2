"""Audit every active Polymarket temperature market for its resolution
station, join against foundation.station_db, and emit:
  - a markdown table you can paste into the README
  - the YAML stanza for config.yaml's strategies.weather.cities

Re-run whenever Polymarket adds a city. Any city whose ICAO is not in
foundation/station_db.py is reported as MISSING - add the coords there
first, then re-run this script.

Usage:
  python tools/audit_stations.py
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from typing import Iterable

import os

import requests

# Make tools/ a runnable entrypoint even though the package isn't on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foundation.station_db import STATIONS, lookup  # noqa: E402


TAGS = ["highest-temperature", "lowest-temperature"]
GAMMA = "https://gamma-api.polymarket.com"


def fetch_all(tag_slug: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        r = requests.get(f"{GAMMA}/events",
                         params={"closed": "false", "active": "true",
                                 "limit": 100, "tag_slug": tag_slug,
                                 "offset": offset}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json() or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return out


def parse_city(slug: str) -> str | None:
    m = re.match(r"(?:highest|lowest)-temperature-in-([a-z0-9-]+?)-on-", slug)
    return m.group(1) if m else None


def parse_station(desc: str) -> dict:
    if not desc:
        return {}
    # Examples seen:
    #   "recorded at the LaGuardia Airport Station"
    #   "recorded at the London City Airport Station"
    nm = re.search(r"recorded at the\s+(.+?)\s+(?:Airport\s+)?Station", desc)
    url = re.search(
        r"https?://(?:www\.)?wunderground\.com/history/daily/[a-z]{2}/(?:[^/\s]+/){1,3}[A-Z]{3,4}",
        desc,
    )
    icao = re.search(
        r"wunderground\.com/history/daily/(?:[a-z]{2}/(?:[^/\s]+/){0,3})([A-Z]{3,4})\b",
        desc,
    )
    cc = re.search(r"wunderground\.com/history/daily/([a-z]{2})/", desc)
    return {
        "name": nm.group(1).strip() if nm else None,
        "url": url.group(0) if url else None,
        "icao": icao.group(1) if icao else None,
        "cc": cc.group(1).upper() if cc else None,
    }


def main() -> int:
    by_city: dict[str, dict] = {}
    for tag in TAGS:
        for e in fetch_all(tag):
            slug = e.get("slug") or ""
            city = parse_city(slug)
            if not city:
                continue
            st = parse_station(e.get("description") or "")
            if not st.get("icao"):
                continue
            row = by_city.setdefault(city, {
                "city": city, "kinds": set(),
                "station_name": st["name"], "icao": st["icao"],
                "cc": st["cc"], "url": st["url"],
            })
            row["kinds"].add("max" if tag == "highest-temperature" else "min")

    # Validate every station against the DB
    cities = sorted(by_city.values(), key=lambda r: r["city"])
    missing = [r for r in cities if lookup(r["icao"]) is None]

    print("# City / station audit\n")
    print("| city | station name | ICAO | CC | kinds | lat | lon | tz | "
          "DB? |")
    print("|------|---------------|------|----|-------|------|------|----|----|")
    for r in cities:
        s = lookup(r["icao"])
        if s:
            lat, lon, tz, ok = f"{s.lat:.4f}", f"{s.lon:.4f}", s.timezone, "OK"
        else:
            lat = lon = tz = "?"; ok = "MISSING"
        kinds = "+".join(sorted(r["kinds"]))
        print(f"| {r['city']} | {r['station_name']} | {r['icao']} | "
              f"{r['cc']} | {kinds} | {lat} | {lon} | {tz} | {ok} |")
    print()
    print(f"Total cities: {len(cities)}; missing from station DB: "
          f"{len(missing)}")
    if missing:
        print("\nAdd these to foundation/station_db.py:")
        for r in missing:
            print(f"  {r['icao']:6s}  {r['city']:25s}  {r['station_name']}  "
                  f"({r['cc']})  url={r['url']}")

    print("\n\n# config.yaml stanza (paste under strategies.weather.cities):\n")
    print("    cities:")
    for r in cities:
        s = lookup(r["icao"])
        if not s:
            continue
        # Build a generous slug-hint list: the city slug + the ICAO lowered.
        hints = sorted({r["city"], r["icao"].lower()})
        kinds = sorted(r["kinds"])
        print(f"      - slug_hints: [{', '.join(hints)}]")
        print(f"        city: {r['city']}")
        print(f"        station_name: {s.name} ({s.icao})")
        print(f"        lat: {s.lat}")
        print(f"        lon: {s.lon}")
        print(f"        timezone: {s.timezone}")
        print(f"        resolution_source: {r['url']}")
        print(f"        market_kinds: [{', '.join(kinds)}]")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
