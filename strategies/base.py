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
