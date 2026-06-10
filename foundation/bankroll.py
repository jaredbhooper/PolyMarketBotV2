"""Virtual bankroll: enforce per-strategy allocations + full audit trail.

One total bankroll, split into per-strategy allocations by percent. Each
strategy's commit path calls `try_debit(strategy, stake)` BEFORE writing
its position row; on settlement the grader calls `credit(strategy,
proceeds)`. Every debit / credit is logged in `bankroll_transactions`
so any later number on the master report can be reconstructed from the
audit log.

A strategy with an exhausted allocation logs `kind='skipped_no_capital'`
(no actual debit) and reports it on the master scoreboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_ALLOCATIONS = {
    "weather": 0.30,
    "bucket_arb": 0.15,
    "cross_venue_arb": 0.15,
    "copy_trading": 0.20,
    # bucket_arb's multi-mode shares the bucket_arb allocation (it's the
    # same strategy file, same module name).  Prompt B's numeric defaults
    # used "binary arb 15% + multi arb 20%" as separate buckets, but in
    # this codebase both run inside bucket_arb so we collapse them into
    # one 15% bucket and let the operator override per-strategy in
    # config if they want a finer split.
}


def normalize_allocations(allocs: dict[str, float],
                            starting_bankroll: float) -> dict[str, float]:
    """Force allocations to sum to 100% (renormalize) and return per-strategy
    starting allocation in USD."""
    if not allocs:
        return {}
    total = sum(float(v) for v in allocs.values()) or 1.0
    return {k: float(v) / total * starting_bankroll for k, v in allocs.items()}


class Bankroll:
    def __init__(self, cfg: dict, ledger):
        b = (cfg or {}).get("bankroll") or {}
        self.starting_bankroll = float(b.get("starting_bankroll", 1000.0))
        configured = b.get("allocations") or DEFAULT_ALLOCATIONS
        self.allocations_pct = dict(configured)
        self.alloc_usd = normalize_allocations(self.allocations_pct,
                                                 self.starting_bankroll)
        self.ledger = ledger
        self._init_if_needed()

    def _init_if_needed(self) -> None:
        for strategy, alloc in self.alloc_usd.items():
            row = self.ledger.get_bankroll_row(strategy)
            if row is None:
                self.ledger.upsert_bankroll_row(
                    strategy, pct=self.allocations_pct[strategy],
                    starting=alloc, cash=alloc, exposure=0.0)
                self.ledger.record_bankroll_txn(
                    strategy, "init", alloc,
                    related_table=None, related_id=None,
                    cash_after=alloc, exposure_after=0.0,
                    note="initial allocation")

    def try_debit(self, strategy: str, amount: float,
                    related_table: str | None = None,
                    related_id: int | None = None,
                    note: str | None = None) -> bool:
        """Reserve `amount` of capital for an opening trade. Returns True
        if debited, False if the strategy is out of allocation (logged
        as kind='skipped_no_capital')."""
        if strategy not in self.alloc_usd:
            # Strategy without an allocation runs in unconstrained mode
            # (back-compat). Returns True so it can trade freely.
            return True
        row = self.ledger.get_bankroll_row(strategy)
        if row is None:
            return False
        cash = float(row["current_cash_usd"])
        exposure = float(row["open_exposure_usd"])
        available = cash - exposure
        if available < amount - 1e-9:
            self.ledger.record_bankroll_txn(
                strategy, "skipped_no_capital", float(amount),
                related_table=related_table, related_id=related_id,
                cash_after=cash, exposure_after=exposure,
                note=note or f"available ${available:.2f} < requested ${amount:.2f}")
            return False
        new_exposure = exposure + amount
        self.ledger.upsert_bankroll_row(strategy,
                                         pct=float(row["pct"]),
                                         starting=float(row["starting_alloc_usd"]),
                                         cash=cash, exposure=new_exposure)
        self.ledger.record_bankroll_txn(
            strategy, "debit", float(amount),
            related_table=related_table, related_id=related_id,
            cash_after=cash, exposure_after=new_exposure, note=note)
        return True

    def credit(self, strategy: str, proceeds: float, opening_stake: float,
                 related_table: str | None = None,
                 related_id: int | None = None,
                 note: str | None = None) -> None:
        """At settlement: release the original `opening_stake` from
        exposure, credit `proceeds` to cash. Net P&L = proceeds - stake."""
        if strategy not in self.alloc_usd:
            return
        row = self.ledger.get_bankroll_row(strategy)
        if row is None:
            return
        cash = float(row["current_cash_usd"])
        exposure = float(row["open_exposure_usd"])
        new_exposure = max(0.0, exposure - opening_stake)
        new_cash = cash - opening_stake + proceeds
        self.ledger.upsert_bankroll_row(strategy,
                                         pct=float(row["pct"]),
                                         starting=float(row["starting_alloc_usd"]),
                                         cash=new_cash,
                                         exposure=new_exposure)
        self.ledger.record_bankroll_txn(
            strategy, "credit", float(proceeds),
            related_table=related_table, related_id=related_id,
            cash_after=new_cash, exposure_after=new_exposure, note=note)

    def snapshot(self) -> list[dict[str, Any]]:
        """Returns the current per-strategy bankroll state for the
        master report scoreboard."""
        out: list[dict[str, Any]] = []
        for row in self.ledger.list_bankroll_rows():
            d = dict(row)
            d["available_usd"] = float(row["current_cash_usd"]) - float(row["open_exposure_usd"])
            out.append(d)
        return out
