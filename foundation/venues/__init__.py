"""Venue abstraction. A VenueMarket is one binary YES/NO contract on one
prediction-market venue (Polymarket or Kalshi). The cross-venue
arbitrage strategy works at this layer - it pairs up VenueMarkets that
reference the same real-world event across different venues.
"""
from foundation.venues.base import VenueMarket, Venue
from foundation.venues.polymarket import PolymarketVenue
from foundation.venues.kalshi import KalshiVenue

__all__ = ["VenueMarket", "Venue", "PolymarketVenue", "KalshiVenue"]
