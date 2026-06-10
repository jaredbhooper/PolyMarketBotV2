"""Cross-venue normalized market interface.

A VenueMarket carries every field the cross-venue arb strategy needs to
(a) match the contract to its counterpart on the other venue, (b) compare
resolution rules, and (c) book-walk for paper fills. The shape is the
intersection of what Polymarket and Kalshi expose; venue-specific quirks
go in `extras`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VenueMarket:
    venue: str                              # 'polymarket' | 'kalshi'
    venue_market_id: str                    # ticker (kalshi) or conditionId (polymarket)
    venue_event_id: str                     # event_ticker or event_id
    title: str                              # human-readable title (full)
    leg_title: str                          # bucket label (e.g. '14C', '90-91F', 'Trump')
    rules_text: str                         # full rules / resolution text
    settlement_source: str                  # human note: "Wunderground EGLC" / "NWS Climatological Report NYC"
    settlement_source_url: str | None       # canonical URL when available
    close_time_iso: str | None              # ISO trading-close timestamp
    # Order book - same convention for both venues:
    #   yes_asks: ascending price [{price, size}] - buying YES walks this
    #   no_asks:  ascending price [{price, size}] - buying NO walks this
    yes_asks: list[dict] = field(default_factory=list)
    no_asks: list[dict] = field(default_factory=list)
    yes_bid: float | None = None
    no_bid: float | None = None
    # Fees. Polymarket: zero on book trades for now (paper). Kalshi: quadratic fee.
    fee_model: dict = field(default_factory=lambda: {"type": "flat", "per_contract": 0.0})
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def yes_ask(self) -> float | None:
        return self.yes_asks[0]["price"] if self.yes_asks else None

    @property
    def no_ask(self) -> float | None:
        return self.no_asks[0]["price"] if self.no_asks else None


class Venue(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_markets(self, category_hint: str | None = None
                       ) -> list[VenueMarket]:
        """Return every reachable binary market the venue exposes that's
        relevant to the cross-venue arb scope. Implementations may filter
        (e.g. only Climate/Weather + Politics + Sports) for cost control.
        """
