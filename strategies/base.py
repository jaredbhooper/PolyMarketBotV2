"""Strategy contract (section 0 of the build plan).

A strategy NEVER touches the ledger, executor, or prices directly. It only
prices probability. The foundation decides whether that probability is an
edge worth (paper) trading.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Market:
    """The shape the scanner hands strategies. Strategy-agnostic."""
    market_id: str                 # Polymarket condition_id (stable across price moves)
    slug: str
    question: str
    category: str
    rules_text: str
    resolve_date: str | None       # ISO date, may be None for ongoing
    end_date_iso: str | None
    # Per-outcome token IDs for YES/NO and current top of book + depth.
    yes_token_id: str | None
    no_token_id: str | None
    yes_ask: float | None
    yes_bid: float | None
    no_ask: float | None
    no_bid: float | None
    yes_book: list[dict] = field(default_factory=list)   # [{price, size}, ...] asks
    no_book: list[dict] = field(default_factory=list)
    yes_book_bids: list[dict] = field(default_factory=list)
    no_book_bids: list[dict] = field(default_factory=list)
    book_depth_usd: float = 0.0
    # Free-form strategy hints parsed out of the slug (threshold, unit, etc).
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Estimate:
    """What a strategy emits. metadata is stored as JSON in the signals table."""
    p_final: float                 # clamped to [0.02, 0.98] by the strategy
    confidence: float = 1.0        # 0..1; foundation multiplies stake by this
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"
    edge_threshold: float = 0.08
    kelly_fraction: float = 0.15

    @abstractmethod
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        """Filter the scanner's full market list down to ones this strategy
        knows how to price."""

    @abstractmethod
    def estimate(self, market: Market) -> Estimate | None:
        """Return a probability estimate, or None to skip this market."""

    def resolve(self, market: Market, settled_at: str) -> dict | None:
        """Optional: strategy-specific truth fetcher used by the grader.
        Default returns None; the grader falls back to its generic path.
        """
        return None


# ---------------------------------------------------------------- arb types
# These extend the Strategy contract to event-level (multi-outcome) work.
# A strategy that overrides `scan_arb()` is routed by main.py through the
# event-walking code path instead of the per-market estimate path.

@dataclass
class ArbLeg:
    """One outcome in a multi-outcome (negRisk) Polymarket event."""
    market_id: str                 # conditionId (per-leg)
    leg_title: str                 # groupItemTitle (e.g. '14C', 'Trump')
    yes_token_id: str | None
    no_token_id: str | None
    yes_asks: list[dict] = field(default_factory=list)   # ascending price
    yes_bids: list[dict] = field(default_factory=list)   # descending price
    no_asks: list[dict] = field(default_factory=list)
    no_bids: list[dict] = field(default_factory=list)
    gamma_yes_ask: float | None = None     # snapshot from Gamma (lagged)
    gamma_yes_bid: float | None = None
    end_date_iso: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArbEvent:
    """A grouped multi-outcome event - one MECE set of legs."""
    event_id: str
    event_slug: str
    event_title: str
    end_date_iso: str | None
    neg_risk: bool                          # Polymarket's MECE flag
    legs: list[ArbLeg] = field(default_factory=list)
    completeness_verified: bool = False
    completeness_note: str = ""
    books_fetched: bool = False             # True iff all leg books were walked
    extras: dict[str, Any] = field(default_factory=dict)
