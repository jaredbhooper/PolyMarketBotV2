"""Health monitor: per-strategy heartbeats + top-level exception
isolation so a failure in one strategy never blocks another.

Use as a context manager:

    with HealthSession(ledger, "weather") as h:
        h.markets_scanned = len(universe)
        h.fills = result["filled"]
        ... run the strategy ...
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any


class HealthSession:
    def __init__(self, ledger, strategy: str):
        self.ledger = ledger
        self.strategy = strategy
        self.markets_scanned: int = 0
        self.fills: int = 0
        self.extras: dict[str, Any] = {}
        self._start = 0.0
        self._error: str | None = None

    def __enter__(self) -> "HealthSession":
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        ok = exc_type is None
        if not ok:
            self._error = (f"{exc_type.__name__}: {exc_val}\n"
                            + "".join(traceback.format_tb(exc_tb))[:2000])
        try:
            self.ledger.record_health(
                strategy=self.strategy, ok=ok,
                duration_s=time.time() - self._start,
                markets_scanned=self.markets_scanned,
                fills=self.fills,
                error_text=self._error,
                extras=self.extras,
            )
        except Exception:
            pass
        # Always swallow the exception so other strategies still run.
        return True


def banner(ledger, stale_after_hours: dict[str, float] | None = None
             ) -> str:
    """Return a single-line banner summarizing health of all strategies.

    stale_after_hours: per-strategy threshold; if no OK heartbeat
    occurred within that many hours, the strategy is flagged stale.
    Default 6h for every strategy.
    """
    stale_after_hours = stale_after_hours or {}
    rows = ledger.latest_health_per_strategy()
    warnings: list[str] = []
    for r in rows:
        strat = r["strategy"]
        ok = bool(r["ok"])
        hours_ago = _hours_since(r["ts"])
        thresh = float(stale_after_hours.get(strat, 6.0))
        if not ok:
            warnings.append(f"{strat}: last run errored ({(r['error_text'] or '')[:60]})")
        elif hours_ago > thresh:
            warnings.append(f"{strat}: no successful run in {hours_ago:.1f}h")
    if not warnings:
        return "HEALTH: OK"
    return "HEALTH: " + " | ".join(warnings)


def _hours_since(ts_iso: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (ValueError, TypeError, AttributeError):
        return 9999.0
