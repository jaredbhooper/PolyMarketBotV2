"""Wallet autopsy: derive a behavioral fingerprint from public trade
history and classify into one of seven archetypes.

Read-only. Reuses the existing `wallet_trades` cache (no new API calls
if the wallet's history is already pulled). The /trades endpoint does
NOT expose order maker/taker flags, so we proxy market-making with the
two-sided-quoting signature instead of a direct maker/taker ratio.

Public surface:
  fingerprint(trades, now_ts=None) -> dict
  classify(fp, cfg=None) -> (archetype, confidence, evidence)
  autopsy(wallet, ledger, polymarket_data=None, cfg=None, refresh=False) -> dict
  autopsy_top(ledger, polymarket_data=None, cfg=None, n=20) -> dict

Archetypes:
  speed-reactor     - sub-minute median inter-trade interval, broad category mix
  market-maker      - frequent two-sided quoting (BUY+SELL same asset, short window)
  arbitrageur       - many simultaneous-offsetting cross-market BUYs
  sharp-line-taker  - decisive mid-price entries held to resolution, moderate cadence
  niche-judgment    - one category dominates, infrequent, long holds
  endgame-grinder   - majority of entries at extreme prices (>0.85 or <0.15)
  mixed             - no single signature dominates

Each classification carries a confidence in [0,1] and a list of
evidence strings naming the signals that fired.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from strategies.copy_trading import compute_metrics, _category_from_slug


# -------------------------------------------------------------- defaults
DEFAULT_AUTOPSY_CFG: dict[str, Any] = {
    # Window inside which a same-asset (BUY, SELL) pair counts as MM quoting.
    "mm_window_sec": 300,
    # Window inside which a different-asset (BUY, BUY) pair counts as arb.
    "arb_window_sec": 120,
    # Speed-reactor: median inter-trade interval below this is "fast".
    "speed_max_median_interval_sec": 60.0,
    # Speed-reactor: needs at least this many distinct categories active.
    "speed_min_categories": 3,
    # Market-maker: two-sided rate above this triggers MM.
    "mm_min_two_sided_rate": 0.20,
    # Arbitrageur: cross-market BUY-pair rate above this triggers arb.
    "arb_min_pair_rate": 0.30,
    # Niche-judgment: dominant category share and median hold time.
    "niche_min_dominant_share": 0.70,
    "niche_min_median_hold_days": 5.0,
    # Endgame-grinder: share of entries at extreme prices.
    "endgame_min_extreme_share": 0.50,
    "endgame_extreme_low": 0.15,
    "endgame_extreme_high": 0.85,
    # Sharp-line-taker: held-to-end share + mid-price entries.
    "sharp_min_held_to_end": 0.60,
    "sharp_entry_price_lo": 0.30,
    "sharp_entry_price_hi": 0.70,
    # Minimum trades required to attempt a non-mixed classification.
    "min_trades_for_classification": 30,
}


# Closest existing strategy in *our* lineup + copyability note.
ARCHETYPE_ANALOGUE: dict[str, dict[str, str]] = {
    "speed-reactor": {
        "analogue": "no close analogue (closest: sharpline, but sub-second latency required)",
        "copyable": "NOT copyable at our 5-min cycle latency",
    },
    "market-maker": {
        "analogue": "lp_sim",
        "copyable": "NOT directly copyable; useful as a benchmark for lp_sim quoting",
    },
    "arbitrageur": {
        "analogue": "bucket_arb + cross_venue_arb",
        "copyable": "NOT copyable directly (needs simultaneous fills), but signals which markets diverge",
    },
    "sharp-line-taker": {
        "analogue": "sharpline + copy_trading",
        "copyable": "COPYABLE - holds to resolution; copy_trading scout already catches this profile",
    },
    "niche-judgment": {
        "analogue": "weather (if niche==weather) or copy_trading otherwise",
        "copyable": "COPYABLE - long holds tolerate our cycle latency",
    },
    "endgame-grinder": {
        "analogue": "no close analogue",
        "copyable": "PARTIAL - copyable only if our cycle covers the relevant near-resolution markets",
    },
    "mixed": {
        "analogue": "copy_trading (generic)",
        "copyable": "case-by-case",
    },
}


# -------------------------------------------------------------- helpers
def _shannon_entropy(counts: dict[Any, float]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for v in counts.values():
        if v <= 0:
            continue
        p = v / total
        h -= p * math.log2(p)
    return h


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def _category_of(t: dict) -> str:
    return _category_from_slug(t.get("slug") or t.get("eventSlug") or t.get("title"))


# -------------------------------------------------------------- fingerprint
def fingerprint(trades: list[dict], now_ts: int | None = None,
                cfg: dict | None = None) -> dict[str, Any]:
    """Build the behavioral fingerprint. Trades must be the raw dicts
    returned by Polymarket /trades (or the cached equivalent stored in
    wallet_trades.raw_json)."""
    c = {**DEFAULT_AUTOPSY_CFG, **(cfg or {})}
    if now_ts is None:
        now_ts = int(time.time())
    base = compute_metrics(trades, now_ts=now_ts)
    fp: dict[str, Any] = dict(base)
    fp["n_trades"] = len(trades)
    if not trades:
        fp.update({
            "median_interval_sec": float("inf"),
            "interval_cv": 0.0,
            "hour_entropy": 0.0,
            "hour_entropy_norm": 0.0,
            "share_overnight": 0.0,
            "n_categories_active": 0,
            "entry_price_mean": 0.0, "entry_price_std": 0.0,
            "entry_price_p10": 0.0, "entry_price_p50": 0.0, "entry_price_p90": 0.0,
            "share_entry_extreme": 0.0,
            "two_sided_pairs": 0, "two_sided_rate": 0.0,
            "offsetting_pairs": 0, "offsetting_rate": 0.0,
            "median_hold_days": 0.0, "pct_held_to_end": 0.0,
            "stake_uniformity": 0.0,
            "trades_per_active_day": 0.0,
            "archetype_inputs_ok": False,
        })
        return fp

    rows = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))

    # --- inter-trade intervals
    intervals = []
    for a, b in zip(rows, rows[1:]):
        dt = int(b.get("timestamp") or 0) - int(a.get("timestamp") or 0)
        if dt > 0:
            intervals.append(dt)
    if intervals:
        median_int = sorted(intervals)[len(intervals) // 2]
        mean_int = sum(intervals) / len(intervals)
        var = sum((x - mean_int) ** 2 for x in intervals) / max(1, len(intervals) - 1)
        sd_int = math.sqrt(var)
        cv = sd_int / mean_int if mean_int > 0 else 0.0
    else:
        median_int = float("inf")
        cv = 0.0

    # --- hour-of-day distribution
    hour_counts: dict[int, int] = defaultdict(int)
    overnight = 0
    for t in rows:
        ts = int(t.get("timestamp") or 0)
        if ts <= 0:
            continue
        h = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        hour_counts[h] += 1
        if 0 <= h < 6:
            overnight += 1
    h_ent = _shannon_entropy(hour_counts)
    h_ent_norm = h_ent / math.log2(24)  # in [0,1]
    share_overnight = overnight / len(rows)

    # --- entry-price distribution (BUY side only)
    buy_prices = [float(t.get("price") or 0.0) for t in rows
                  if (t.get("side") or "").upper() == "BUY"]
    if buy_prices:
        mean_p = sum(buy_prices) / len(buy_prices)
        var_p = sum((p - mean_p) ** 2 for p in buy_prices) / max(1, len(buy_prices) - 1)
        sd_p = math.sqrt(var_p)
        p10 = _percentile(buy_prices, 0.10)
        p50 = _percentile(buy_prices, 0.50)
        p90 = _percentile(buy_prices, 0.90)
        lo = c["endgame_extreme_low"]
        hi = c["endgame_extreme_high"]
        extreme = sum(1 for p in buy_prices if p < lo or p > hi)
        share_extreme = extreme / len(buy_prices)
    else:
        mean_p = sd_p = p10 = p50 = p90 = 0.0
        share_extreme = 0.0

    # --- per-asset trail: BUYs build cost basis, SELLs close.
    asset_events: dict[str, list[tuple[int, str, float, float]]] = defaultdict(list)
    for t in rows:
        side = (t.get("side") or "").upper()
        asset = t.get("asset") or ""
        if not asset:
            continue
        asset_events[asset].append((
            int(t.get("timestamp") or 0), side,
            float(t.get("size") or 0.0), float(t.get("price") or 0.0),
        ))

    # --- two-sided quoting (MM signature): same-asset (BUY, SELL) within window.
    mm_window = int(c["mm_window_sec"])
    two_sided = 0
    for asset, ev in asset_events.items():
        # Walk events; count pairs of (BUY, SELL) within window in either direction.
        for i in range(len(ev)):
            ts_i, side_i, _, _ = ev[i]
            for j in range(i + 1, len(ev)):
                ts_j, side_j, _, _ = ev[j]
                if ts_j - ts_i > mm_window:
                    break
                if {side_i, side_j} == {"BUY", "SELL"}:
                    two_sided += 1
                    break  # one pair per i is enough; avoid quadratic explosion
    two_sided_rate = two_sided / len(rows)

    # --- simultaneous-offsetting cross-market legs (arb signature).
    # Real cross-market arb pairs trades on DIFFERENT assets that share
    # an event (same slug / event_slug) within arb_window_sec. Pairs
    # that span different events are just fast trading, not arb.
    arb_window = int(c["arb_window_sec"])
    buys = [(int(t.get("timestamp") or 0), t.get("asset") or "",
             t.get("conditionId") or "",
             (t.get("slug") or t.get("eventSlug") or ""))
            for t in rows if (t.get("side") or "").upper() == "BUY"]
    buys.sort(key=lambda x: x[0])
    offsetting = 0
    for i in range(len(buys)):
        ts_i, asset_i, cond_i, slug_i = buys[i]
        for j in range(i + 1, len(buys)):
            ts_j, asset_j, cond_j, slug_j = buys[j]
            if ts_j - ts_i > arb_window:
                break
            if (asset_i and asset_j and asset_i != asset_j
                    and slug_i and slug_i == slug_j):
                offsetting += 1
                break
    offsetting_rate = offsetting / len(rows)

    # --- hold time + held-to-end share.
    hold_seconds: list[float] = []
    n_assets = 0
    n_held_to_end = 0
    last_ts = max(int(t.get("timestamp") or 0) for t in rows)
    for asset, ev in asset_events.items():
        first_buy = next((ts for ts, side, *_ in ev if side == "BUY"), None)
        first_sell = next((ts for ts, side, *_ in ev if side == "SELL"), None)
        if first_buy is None:
            continue
        n_assets += 1
        if first_sell is not None and first_sell >= first_buy:
            hold_seconds.append(float(first_sell - first_buy))
        else:
            hold_seconds.append(float(last_ts - first_buy))
            n_held_to_end += 1
    median_hold_days = (sorted(hold_seconds)[len(hold_seconds) // 2] / 86400.0
                        if hold_seconds else 0.0)
    pct_held_to_end = (n_held_to_end / n_assets) if n_assets > 0 else 0.0

    # --- stake uniformity (USD notional).
    notionals = [float(t.get("size") or 0.0) * float(t.get("price") or 0.0)
                 for t in rows]
    notionals = [n for n in notionals if n > 0]
    if len(notionals) >= 2:
        m = sum(notionals) / len(notionals)
        v = sum((n - m) ** 2 for n in notionals) / (len(notionals) - 1)
        sd = math.sqrt(v)
        # Uniformity: 1.0 when stake is identical, 0 when CV is >=1.
        stake_cv = sd / m if m > 0 else 0.0
        stake_uniformity = max(0.0, 1.0 - min(1.0, stake_cv))
    else:
        stake_uniformity = 0.0

    # --- trades-per-active-day
    day_set = set()
    for t in rows:
        ts = int(t.get("timestamp") or 0)
        if ts <= 0:
            continue
        day_set.add(datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat())
    active_days = max(1, len(day_set))
    trades_per_day = len(rows) / active_days

    # --- distinct categories active
    cats = set()
    for t in rows:
        cats.add(_category_of(t))
    n_cats = len([c for c in cats if c])

    fp.update({
        "median_interval_sec": float(median_int) if median_int != float("inf") else float("inf"),
        "interval_cv": cv,
        "hour_entropy": h_ent,
        "hour_entropy_norm": h_ent_norm,
        "share_overnight": share_overnight,
        "n_categories_active": n_cats,
        "entry_price_mean": mean_p,
        "entry_price_std": sd_p,
        "entry_price_p10": p10, "entry_price_p50": p50, "entry_price_p90": p90,
        "share_entry_extreme": share_extreme,
        "two_sided_pairs": two_sided, "two_sided_rate": two_sided_rate,
        "offsetting_pairs": offsetting, "offsetting_rate": offsetting_rate,
        "median_hold_days": median_hold_days,
        "pct_held_to_end": pct_held_to_end,
        "stake_uniformity": stake_uniformity,
        "trades_per_active_day": trades_per_day,
        "archetype_inputs_ok": True,
    })
    return fp


# -------------------------------------------------------------- classifier
def classify(fp: dict, cfg: dict | None = None) -> tuple[str, float, list[str]]:
    """Decision-tree classifier. Highest-signal-first. Returns
    (archetype, confidence in [0,1], evidence list)."""
    c = {**DEFAULT_AUTOPSY_CFG, **(cfg or {})}
    n = fp.get("n_trades", 0)
    if not fp.get("archetype_inputs_ok") or n < c["min_trades_for_classification"]:
        return ("mixed", 0.0,
                [f"insufficient_data: n_trades={n} < {c['min_trades_for_classification']}"])

    evidence: list[str] = []

    # 1. Market-maker: two-sided quoting on same asset.
    mm_rate = fp["two_sided_rate"]
    if mm_rate >= c["mm_min_two_sided_rate"]:
        conf = min(1.0, mm_rate / c["mm_min_two_sided_rate"])
        evidence.append(f"two_sided_rate={mm_rate:.2f} >= {c['mm_min_two_sided_rate']:.2f}")
        if fp["interval_cv"] < 1.0:
            evidence.append(f"low_interval_cv={fp['interval_cv']:.2f} (regular cadence)")
        return ("market-maker", conf, evidence)

    # 2. Arbitrageur: simultaneous cross-market BUYs.
    arb_rate = fp["offsetting_rate"]
    if arb_rate >= c["arb_min_pair_rate"]:
        conf = min(1.0, arb_rate / c["arb_min_pair_rate"])
        evidence.append(
            f"offsetting_rate={arb_rate:.2f} >= {c['arb_min_pair_rate']:.2f} "
            f"({fp['offsetting_pairs']} cross-market BUY pairs within {c['arb_window_sec']}s)")
        return ("arbitrageur", conf, evidence)

    # 3. Speed-reactor: fast cadence, broad category mix.
    if (fp["median_interval_sec"] < c["speed_max_median_interval_sec"]
            and fp["n_categories_active"] >= c["speed_min_categories"]):
        conf_a = min(1.0, c["speed_max_median_interval_sec"] / max(1.0, fp["median_interval_sec"]))
        conf_b = min(1.0, fp["n_categories_active"] / (c["speed_min_categories"] + 2))
        conf = (conf_a + conf_b) / 2.0
        evidence.append(
            f"median_interval={fp['median_interval_sec']:.0f}s < "
            f"{c['speed_max_median_interval_sec']:.0f}s")
        evidence.append(f"n_categories_active={fp['n_categories_active']}")
        return ("speed-reactor", conf, evidence)

    # 4. Niche-judgment: single category, long holds, infrequent.
    if (fp["dominant_share"] >= c["niche_min_dominant_share"]
            and fp["median_hold_days"] >= c["niche_min_median_hold_days"]):
        conf = min(1.0, (fp["dominant_share"] + min(1.0, fp["median_hold_days"] / 30.0)) / 2.0)
        evidence.append(
            f"dominant_category={fp['dominant_category']} "
            f"share={fp['dominant_share']:.2f} >= {c['niche_min_dominant_share']:.2f}")
        evidence.append(f"median_hold={fp['median_hold_days']:.1f}d "
                        f">= {c['niche_min_median_hold_days']:.1f}d")
        return ("niche-judgment", conf, evidence)

    # 5. Endgame-grinder: most entries at extreme prices.
    if fp["share_entry_extreme"] >= c["endgame_min_extreme_share"]:
        conf = min(1.0, fp["share_entry_extreme"] / c["endgame_min_extreme_share"])
        evidence.append(
            f"share_entry_extreme={fp['share_entry_extreme']:.2f} "
            f">= {c['endgame_min_extreme_share']:.2f} "
            f"(entries at p<{c['endgame_extreme_low']} or p>{c['endgame_extreme_high']})")
        return ("endgame-grinder", conf, evidence)

    # 6. Sharp-line-taker: holds to resolution + mid-price entries.
    p50 = fp["entry_price_p50"]
    if (fp["pct_held_to_end"] >= c["sharp_min_held_to_end"]
            and c["sharp_entry_price_lo"] <= p50 <= c["sharp_entry_price_hi"]):
        conf = min(1.0, (fp["pct_held_to_end"] + 0.5) / 1.5)
        evidence.append(f"pct_held_to_end={fp['pct_held_to_end']:.2f} "
                        f">= {c['sharp_min_held_to_end']:.2f}")
        evidence.append(f"entry_price_median={p50:.2f} (mid-range)")
        return ("sharp-line-taker", conf, evidence)

    # 7. Mixed default.
    evidence.append("no single signal cleared threshold")
    return ("mixed", 0.2, evidence)


# -------------------------------------------------------------- pipeline
def autopsy(wallet: str, ledger, polymarket_data=None,
            cfg: dict | None = None, refresh: bool = False) -> dict[str, Any]:
    """Pull (or reuse) the wallet's cached trade history, build the
    fingerprint, classify. `refresh=True` triggers an incremental pull
    via the provided polymarket_data client."""
    wallet = wallet.lower()
    if refresh and polymarket_data is not None:
        cursor = ledger.get_wallet_cursor(wallet)
        new_rows = []
        try:
            for t in polymarket_data.iter_trades(user=wallet, page=100,
                                                  max_pages=20,
                                                  stop_before_ts=cursor):
                new_rows.append(t)
        except RuntimeError:
            pass
        if new_rows:
            ledger.upsert_wallet_trades(wallet, new_rows)
            ledger.set_wallet_cursor(
                wallet, max(int(r.get("timestamp") or 0) for r in new_rows))
    trades = ledger.get_wallet_trades(wallet)
    fp = fingerprint(trades, cfg=cfg)
    archetype, confidence, evidence = classify(fp, cfg=cfg)
    analogue = ARCHETYPE_ANALOGUE[archetype]
    return {
        "wallet": wallet,
        "n_trades": fp["n_trades"],
        "fingerprint": fp,
        "archetype": archetype,
        "confidence": confidence,
        "evidence": evidence,
        "closest_strategy": analogue["analogue"],
        "copyability": analogue["copyable"],
    }


def autopsy_top(ledger, polymarket_data=None, cfg: dict | None = None,
                n: int = 20, refresh: bool = False) -> dict[str, Any]:
    """Run autopsy over the top-N wallets by realized P&L from the
    candidate pool (i.e. anything we already have in `wallets` metrics).
    Returns archetype census + per-wallet verdicts."""
    rows = ledger.list_wallets_top_by_realized(limit=n)
    results = []
    for r in rows:
        wallet = r["wallet"]
        try:
            res = autopsy(wallet, ledger, polymarket_data=polymarket_data,
                          cfg=cfg, refresh=refresh)
        except Exception as e:
            res = {"wallet": wallet, "error": str(e), "archetype": "mixed",
                   "confidence": 0.0, "evidence": [], "n_trades": 0,
                   "closest_strategy": "n/a", "copyability": "n/a"}
        res["realized_pnl_usd"] = float(r.get("realized_pnl_usd") or 0.0)
        results.append(res)
    census: dict[str, int] = defaultdict(int)
    for r in results:
        census[r["archetype"]] += 1
    return {
        "n_processed": len(results),
        "census": dict(census),
        "results": results,
    }
