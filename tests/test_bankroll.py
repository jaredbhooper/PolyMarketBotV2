"""Bankroll + health + master-report tests (Prompt B)."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.bankroll import Bankroll, normalize_allocations
from foundation.health import HealthSession, banner
from foundation.ledger import Ledger


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def _cfg(start=1000.0, allocs=None):
    return {
        "bankroll": {
            "starting_bankroll": start,
            "allocations": allocs or {"weather": 0.40, "bucket_arb": 0.60},
        }
    }


# ---------------------------------------------------------- normalize
def test_normalize_allocations_sums_to_one():
    out = normalize_allocations({"a": 0.40, "b": 0.30}, 1000.0)
    # Renormalized 4/7, 3/7
    assert abs(out["a"] - 1000.0 * 4 / 7) < 1e-6
    assert abs(out["b"] - 1000.0 * 3 / 7) < 1e-6
    assert abs(sum(out.values()) - 1000.0) < 1e-6


# ---------------------------------------------------------- allocation
def test_bankroll_init_writes_per_strategy_rows():
    ledger, path = _temp_ledger()
    br = Bankroll(_cfg(), ledger)
    snap = br.snapshot()
    by = {r["strategy"]: r for r in snap}
    assert abs(by["weather"]["starting_alloc_usd"] - 400.0) < 1e-6
    assert abs(by["bucket_arb"]["starting_alloc_usd"] - 600.0) < 1e-6
    # All cash, zero exposure at init.
    assert by["weather"]["current_cash_usd"] == pytest.approx(400.0)
    assert by["weather"]["open_exposure_usd"] == pytest.approx(0.0)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_try_debit_logs_skipped_no_capital_when_exhausted():
    ledger, path = _temp_ledger()
    br = Bankroll(_cfg(allocs={"weather": 1.0}), ledger)
    # weather alloc = 1000. Debit 600 ok, then 500 should fail.
    assert br.try_debit("weather", 600.0)
    assert not br.try_debit("weather", 500.0)
    txns = ledger.list_bankroll_txns("weather")
    kinds = [t["kind"] for t in txns]
    # init + debit(600) + skipped_no_capital(500)
    assert "init" in kinds and "debit" in kinds and "skipped_no_capital" in kinds
    # The 600 debit shows exposure_after = 600
    debit = [t for t in txns if t["kind"] == "debit"][0]
    assert float(debit["exposure_after_usd"]) == pytest.approx(600.0)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_credit_closes_position_and_updates_cash():
    """Open 300, close with 400 proceeds. Net cash = start - 300 + 400 = start + 100."""
    ledger, path = _temp_ledger()
    br = Bankroll(_cfg(allocs={"weather": 1.0}), ledger)
    assert br.try_debit("weather", 300.0)
    br.credit("weather", proceeds=400.0, opening_stake=300.0)
    snap = {r["strategy"]: r for r in br.snapshot()}
    assert snap["weather"]["current_cash_usd"] == pytest.approx(1100.0)
    assert snap["weather"]["open_exposure_usd"] == pytest.approx(0.0)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_audit_trail_reconstructs_cash_balance():
    ledger, path = _temp_ledger()
    br = Bankroll(_cfg(allocs={"weather": 1.0}), ledger)
    br.try_debit("weather", 200)
    br.credit("weather", 250, 200)        # +50 PnL
    br.try_debit("weather", 100)
    br.credit("weather", 90, 100)         # -10 PnL
    txns = ledger.list_bankroll_txns("weather")
    # Re-derive cash by stepping through txns.
    cash = 0.0
    exposure = 0.0
    for t in txns:
        kind = t["kind"]; amt = float(t["amount_usd"])
        if kind == "init":
            cash = amt
        elif kind == "debit":
            exposure += amt
        elif kind == "credit":
            # credit row's cash_after_usd captures: cash - stake + proceeds
            # we don't store the matching stake on the credit row, so just
            # trust the cash_after_usd column rather than recomputing.
            cash = float(t["cash_after_usd"])
            exposure = float(t["exposure_after_usd"])
    snap = {r["strategy"]: r for r in br.snapshot()}
    # Net PnL = +50 - 10 = 40. Final cash = 1000 + 40 = 1040, exposure = 0.
    assert snap["weather"]["current_cash_usd"] == pytest.approx(1040.0)
    assert snap["weather"]["open_exposure_usd"] == pytest.approx(0.0)
    assert cash == pytest.approx(1040.0)
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------- health
def test_health_session_catches_exception_and_isolates():
    ledger, path = _temp_ledger()
    # Run two strategies; the first raises, the second runs fine.
    with HealthSession(ledger, "weather"):
        raise RuntimeError("boom")
    with HealthSession(ledger, "bucket_arb"):
        pass
    rows = ledger.latest_health_per_strategy()
    by = {r["strategy"]: r for r in rows}
    assert "weather" in by and "bucket_arb" in by
    assert by["weather"]["ok"] == 0
    assert by["bucket_arb"]["ok"] == 1
    assert "boom" in by["weather"]["error_text"]
    try:
        os.unlink(path)
    except OSError:
        pass


def test_banner_warns_on_failed_strategy():
    ledger, path = _temp_ledger()
    with HealthSession(ledger, "weather"):
        raise ValueError("oops")
    text = banner(ledger)
    assert text.startswith("HEALTH: ")
    assert "weather" in text
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------- master report
def test_master_report_renders_with_zero_data(capsys):
    ledger, path = _temp_ledger()
    cfg_path = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    cfg_path.write(f"""
database:
  path: {path.replace(chr(92), '/')}
paper:
  starting_bankroll: 1000
bankroll:
  starting_bankroll: 1000
  allocations:
    weather: 0.30
    bucket_arb: 0.20
active_strategies:
  - module: weather
""")
    cfg_path.close()
    # Master report should run cleanly with no trades at all.
    from foundation.report import print_master_report
    print_master_report(cfg_path.name)
    out = capsys.readouterr().out
    assert "master report" in out
    assert "HEALTH:" in out
    assert "weather" in out
    assert "TOO EARLY" in out      # no settled trades yet
    try:
        os.unlink(path)
        os.unlink(cfg_path.name)
    except OSError:
        pass
