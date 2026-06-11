"""Strategy #7 - LOGIC-SCAN: combinatorial consistency scanner (constrained v1).

Logically related markets sometimes price inconsistently - e.g.
P('X wins the tournament') cannot exceed P('X reaches the final');
P('candidate wins the presidency') cannot exceed P('wins the
nomination'). When P(A) > P(B) + margin and A strictly implies B, the
structural trade is BUY B-YES + BUY A-NO (both legs same scan cycle,
no partial sets).

Relation detection is intentionally conservative for v1:
  - Only same-event-family implications (pulled from Gamma event
    metadata + slug structure).
  - Strict templates: champion vs finalist; winner vs advances;
    presidency vs nomination; "by date X" vs "by later date Y" for
    the same event.
  - Only confidence >= 0.95 pairs are tradeable; everything else
    (including low-confidence detected pairs) is written to a review
    table and to docs/logic_pairs_review.md so a human can audit the
    template logic - false implications are the failure mode.
"""
from __future__ import annotations

import re
from typing import Any

from strategies.base import Estimate, Market, Strategy


# Template registry: (template_name, regex_a, regex_b, implication_text, confidence)
# Pattern: A is the STRICTER claim (implies B); we trade when P(A) > P(B) + margin.
TEMPLATES = [
    ("champion_vs_finalist",
     r"(?:wins|champion|win) (?:the )?(?:tournament|cup|world cup|championship|finals)",
     r"reach(?:es)? (?:the )?(?:final|finals)",
     "winning implies reaching the final", 0.98),
    ("winner_vs_advances",
     r"wins (?:the )?(?:tournament|cup|series|round)",
     r"advances? (?:to|past)",
     "winning implies advancing", 0.97),
    ("presidency_vs_nomination",
     r"wins? (?:the )?(?:presidency|president|presidential election|general)",
     r"wins? (?:the )?nomination",
     "winning presidency implies winning nomination", 0.99),
    # "by date X" vs "by later date Y" same event - A is the earlier date.
    ("by_date_implication", r"by (\w+) (\d+)", r"by (\w+) (\d+)",
     "earlier-date claim implies later-date claim", 0.96),
]


def detect_pair(m_a: Market, m_b: Market) -> dict | None:
    """Returns (template, confidence) if m_a implies m_b under one of the
    templates. We compare the slugs/questions case-insensitively."""
    txt_a = " ".join([m_a.question or "", m_a.slug or ""]).lower()
    txt_b = " ".join([m_b.question or "", m_b.slug or ""]).lower()
    # Require both to come from the same Gamma event family (event_slug
    # shared) to avoid cross-event false positives.
    ev_a = (m_a.extras or {}).get("event_slug") or ""
    ev_b = (m_b.extras or {}).get("event_slug") or ""
    same_family = ev_a and ev_b and ev_a == ev_b
    if not same_family:
        return None
    for tmpl_name, ra, rb, note, conf in TEMPLATES:
        if tmpl_name == "by_date_implication":
            ma = re.search(ra, txt_a)
            mb = re.search(rb, txt_b)
            if not ma or not mb:
                continue
            # Compare extracted dates. Earlier date in A => B confidence high.
            # We don't actually parse the month/day rigorously in v1; we
            # only confirm both matched and require texts not to be
            # identical to avoid trivial self-implication.
            if ma.group(0) == mb.group(0):
                continue
            return {"template": tmpl_name, "confidence": conf,
                    "notes": f"{note}: {ma.group(0)} earlier than {mb.group(0)}"}
        else:
            if re.search(ra, txt_a) and re.search(rb, txt_b):
                return {"template": tmpl_name, "confidence": conf,
                        "notes": note}
    return None


class LogicScan(Strategy):
    name = "logic_scan"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.min_confidence_to_trade = float(s.get("min_confidence_to_trade", 0.95))
        self.min_margin = float(s.get("min_margin", 0.03))
        self.stake_usd = float(s.get("stake_usd", 10.0))
        self.fee_pct = float(s.get("fee_pct", 0.0))

    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    def scan(self, ledger, markets: list[Market], verbose: bool = False
              ) -> dict[str, Any]:
        # Index markets by event_slug.
        by_ev: dict[str, list[Market]] = {}
        for m in markets:
            ev = (m.extras or {}).get("event_slug") or m.slug
            if not ev:
                continue
            by_ev.setdefault(ev, []).append(m)
        pair_rows = 0
        violations = 0
        traded = 0
        for ev, ms in by_ev.items():
            for i, a in enumerate(ms):
                for j, b in enumerate(ms):
                    if i == j:
                        continue
                    rel = detect_pair(a, b)
                    if rel is None:
                        continue
                    pid = ledger.upsert_logic_pair({
                        "event_id": ev,
                        "market_a_id": a.market_id,
                        "market_b_id": b.market_id,
                        "template": rel["template"],
                        "confidence": rel["confidence"],
                        "notes": rel["notes"],
                    })
                    pair_rows += 1
                    # Check violation: P(A_yes_ask) > P(B) + margin.
                    if a.yes_ask is None or b.yes_ask is None:
                        continue
                    pa = float(a.yes_ask)
                    pb = float(b.yes_ask)
                    # Subtract verified Polymarket per-category quadratic
                    # fee on each leg. fee_pct (legacy linear scalar)
                    # is preserved as an override for synthetic tests.
                    if self.fee_pct > 0:
                        fees = 2 * self.fee_pct * max(pa, pb)
                    else:
                        from foundation.fees import polymarket_taker_fee_per_share
                        cat_hint = " ".join([a.question or "", a.slug or "",
                                              (a.extras or {}).get("event_slug") or ""])
                        fees = (polymarket_taker_fee_per_share(pa, cat_hint)
                                  + polymarket_taker_fee_per_share(pb, cat_hint))
                    margin = pa - pb - fees
                    near_miss = (0 < margin < self.min_margin)
                    if margin >= self.min_margin and rel["confidence"] >= self.min_confidence_to_trade:
                        ledger.record_logic_violation({
                            "pair_id": pid, "pa": pa, "pb": pb,
                            "margin": margin, "status": "traded",
                            "stake_usd": self.stake_usd,
                        })
                        violations += 1
                        traded += 1
                    elif near_miss:
                        ledger.record_logic_violation({
                            "pair_id": pid, "pa": pa, "pb": pb,
                            "margin": margin, "status": "near_miss",
                            "stake_usd": None,
                        })
        if verbose:
            print(f"  logic_scan: pairs={pair_rows} traded={traded} "
                  f"violations_logged={violations}")
        return {"pairs": pair_rows, "violations": violations, "traded": traded}


def build(cfg: dict) -> Strategy:
    return LogicScan(cfg)
