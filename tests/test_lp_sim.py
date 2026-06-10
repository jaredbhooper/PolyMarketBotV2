"""LP-SIM (Prompt C, Phase 2) tests."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.base import Market
from strategies.lp_sim import LPSim, score_quote


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def _mk(id_, yes_ask=0.50, yes_bid=0.48, yes_book=None, no_book=None) -> Market:
    return Market(
        market_id=id_, slug=id_, question=id_, category="x",
        rules_text="", resolve_date=None, end_date_iso=None,
        yes_token_id="y", no_token_id="n",
        yes_ask=yes_ask, yes_bid=yes_bid,
        no_ask=1.0 - yes_bid, no_bid=1.0 - yes_ask,
        yes_book=yes_book or [{"price": yes_ask, "size": 100}],
        no_book=no_book or [{"price": 1.0 - yes_bid, "size": 100}],
        extras={},
    )


def test_score_two_sided_beats_one_sided():
    s_two = score_quote(0.01, 25.0, two_sided=True)
    s_one = score_quote(0.01, 25.0, two_sided=False)
    assert s_two > s_one


def test_score_tighter_beats_wider():
    tight = score_quote(0.005, 25.0, two_sided=True)
    wide = score_quote(0.04, 25.0, two_sided=True)
    assert tight > wide


def test_score_zero_when_spread_exceeds_band():
    over = score_quote(0.06, 25.0, two_sided=True, reward_band=0.05)
    assert over == 0.0


def test_run_records_rows_marked_estimate():
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"lp_sim": {"max_markets": 5}}}
    s = LPSim(cfg)
    ms = [_mk(f"m{i}") for i in range(5)]
    res = s.run(ledger, ms, verbose=False)
    assert res["rows_logged"] == 5
    rows = ledger.lp_sim_latest(limit=10)
    assert len(rows) == 5
    assert all(r["estimate_marker"] == "ESTIMATE" for r in rows)
    try:
        os.unlink(path)
    except OSError:
        pass
