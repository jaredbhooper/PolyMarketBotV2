# Build notes (V2 paper-only)

Status of every strategy shipped in V2. None are disabled.

## Active strategies

| # | name             | status | notes |
|---|------------------|--------|-------|
| 1 | weather          | ACTIVE | V1 carry-over; bankroll-wired. |
| 2 | bucket_arb       | ACTIVE | single-leg + multi-mode (`arb_multi` table). |
| 3 | cross_venue_arb  | ACTIVE | Polymarket vs Kalshi; all categories (weather + sports + crypto + politics + economics) since v2; FUZZY pairs never auto-traded; matcher confidence floor 0.9. |
| 3b | cv_probe        | ACTIVE | QUARANTINED research book ($500 virtual side-book). Paper-trades FUZZY pairs to measure divergence rates per category. Never touches main bankroll. |
| 4 | copy_trading     | ACTIVE | Scout via /trades aggregation - leaderboard endpoint not reachable. |
| 5 | sharpline        | ACTIVE | OBSERVE MODE when `ODDS_API_KEY` is empty/missing. Full fill-and-grade lifecycle (RESTING / FILLED / CANCELLED / UNFILLED_RESOLVED) wired to bankroll. |
| 6 | lp_sim           | ACTIVE | static reward score estimate; fills not yet simulated. |
| 7 | logic_scan       | ACTIVE | conservative same-event-family templates only. |

## Resilience-rule disables

None. Every phase reached fully green tests without needing the
disable-via-config fallback.

## Polymarket fee schedule (verified 2026-03)

Sources (search 2026-06-11):
- [Tradetheoutcome — Polymarket Fees Breakdown](https://www.tradetheoutcome.com/polymarket-fees/)
- [KuCoin — Polymarket Fees Explained 2026](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026)
- [PredScope — Polymarket Fees Explained](https://predscope.com/guide/polymarket-fees)
- [QuantJourney — Understanding the Polymarket Fee Curve](https://quantjourney.substack.com/p/understanding-the-polymarket-fee)

Per-category taker rate `r` with quadratic curve
`fee_per_share = r × p × (1 − p)` (peaks at p = 0.50):

| category    | rate    |
|-------------|--------:|
| crypto      | 1.80 %  |
| economics   | 1.50 %  |
| mentions    | 1.56 %  |
| culture     | 1.25 %  |
| weather     | 1.25 %  |
| finance     | 1.00 %  |
| politics    | 1.00 %  |
| tech        | 1.00 %  |
| sports      | 0.75 %  |
| geopolitics | 0.00 %  |
| _unknown_   | 1.00 %  (conservative fallback) |

Maker fills earn a 20-25 % rebate of the counterparty's fee; modeled
as 0 in config (`maker_rebate_pct: 0.00`) by default - flip to 0.20
in `config.yaml` to credit the rebate.

US-regulated Polymarket exchange is a flat 0.30 % taker / 0.20 %
maker rebate. Not enabled in the codebase yet (V1 + V2 connect to
the main international CLOB only); add a `us_regulated` config flag
if/when the exchange splits across the two markets.

The verified schedule lives in `foundation/fees.py:DEFAULT_TAKER_RATES`
and in `config.yaml:polymarket_fees.rates`. A test
(`tests/test_fees.py::test_rate_table_matches_verified_2026_03_schedule`)
fails loudly if the table ever drifts from the source.

## Kalshi fee schedule (verified 2026-04)

Sources (search 2026-06-12):
- [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) (April 2026)
- [marketmath.io — Kalshi Fee Guide 2026](https://marketmath.io/kalshi-fees-2026)

Per-contract quadratic curve with series-level multiplier:

```
taker_fee_per_contract = ceil(multiplier * 0.07 * p * (1-p) * 100) / 100
maker_fee_per_contract = ceil(multiplier * 0.0175 * p * (1-p) * 100) / 100
```

The base rates (`KALSHI_TAKER_BASE = 0.07`, `KALSHI_MAKER_BASE = 0.0175`)
live in `foundation/fees.py` and the per-series `multiplier` is read
from the Kalshi series endpoint's `fee_multiplier` field. Premium series
(crypto, certain sports) carry multipliers > 1.0; the venue adapter
plumbs them through to `kalshi_fee_per_contract`.

A pinned test
(`tests/test_fees.py::test_kalshi_base_rates_pinned_to_2026_04_schedule`)
fails loudly if either base rate drifts, forcing a re-verification.

## v2 cross-venue expansion (2026-06-12)

The cross-venue scanner now covers every category both venues share, not
just daily weather. Six pieces:

1. **Per-category equivalence classifiers** in `foundation/equivalence.py`
   (`classify_pair_sports`, `classify_pair_crypto`, `classify_pair_politics`,
   `classify_pair_economics`) sitting alongside the original
   `classify_pair_weather`. A dispatcher (`classify_pair`) detects the
   pair's category from extras + titles and routes accordingly.
2. **Confidence score** (0..1) returned with every result. The hard
   floor for ANY action (certified trading OR probe) is
   `min_match_confidence = 0.9`. Below that the pair is logged as
   `cv_pairs` but never traded — a mismatched pair (two different games
   paired together) would corrupt the probe's headline statistic.
3. **Multi-category bucketing** in `strategies/cross_venue_arb.py`
   (`bucket_markets_by_category_date`) sits alongside the original
   weather bucket (`bucket_markets_by_key`). The scanner pairs both
   buckets in the same pass; cv_gaps and cv_pairs carry a `category`
   column so per-vertical reporting is cheap.
4. **Time budget** (`cv_scan_budget_minutes`, default 8.0) bounds the
   wall-clock cost of an expanded scan. The deadline check sits in the
   bucket loop, so the scanner degrades gracefully — never overruns the
   cycle workflow's 30-minute window.
5. **CV-PROBE quarantined research book** (`strategies/cv_probe.py`).
   Paper-trades FUZZY pairs to measure how often non-identical referees
   actually disagree. Caps + dedupe + per-category diversity are
   enforced in `CVProbe._apply_caps`. The probe's $500 virtual
   side-book is NOT routed through `Bankroll`, so no bankroll txn is
   ever recorded for a probe trade.
6. **Grader path** (`foundation.grader.grade_cv_probe_positions`) settles
   probe positions with `agreement_outcome` ∈ {AGREED, DIVERGED,
   VOID_MISMATCH, BOTH_VOID}. Voided legs return stake-at-cost. The
   master report's `print_cv_probe_report` renders the per-category
   verdict table.

## Outstanding deferred work

- **LP-Sim fill simulation**: the static reward-score estimator is
  shipped; the strict-price-through fill simulation specified for v2
  is not yet wired (would mirror the Sharpline `simulate_fills_and_grade`
  pattern).
- **Copy-trading leaderboard**: `/leaderboard` on
  data-api.polymarket.com returns 404 (full probe list in
  `docs/api_notes.md`). Scout falls back to /trades aggregation +
  config `seed_wallets`. If/when the endpoint exists, plug it into
  `CopyTrading.discover_candidates` as a third discovery source.

## Workflows (`.github/workflows/`)

Three scheduled workflows. **Files committed locally; never run or
verified from this machine.** Push to a repo with Workflows enabled
to activate:

- `cycle.yml` — `*/30 * * * *`. Runs `python main.py cycle` +
  `logic-scan` + `lp-sim` + `status`. logic-scan and lp-sim live here
  (not in `fast.yml`) because both require a full Polymarket book
  scan, which exceeds fast.yml's 4-minute budget.
- `fast.yml` — `*/5 * * * *`. Runs `follow` + `sharpline-fill-cycle`.
  NEITHER command burns Odds API budget - the follower hits
  `data-api.polymarket.com`; sharpline-fill-cycle only checks the
  CLOB book against already-resting orders. `ODDS_API_KEY` is passed
  in (for symmetry / future expansion) but consumed only by
  `sharpline-post` which runs in `daily.yml`.
- `daily.yml` — `0 10 * * *`. Runs `scout`, `sharpline-post` (the
  only Odds-API-consuming command — sized at 5 sports × 1 cycle/day
  = ~150 req/month, comfortably under the free tier's 450 cap),
  `grade`, `master-report`, then `vacuum` (prunes cache.db beyond
  retention window + VACUUMs both DBs).

All three workflows have a **50 MB guard** on `ledger.db` that fails
the run loudly via `::error::` if the committed file ever exceeds
50 MB. This prevents the 511 MB freelist-bloat incident from
recurring silently.

## Two-DB layout

The ledger lives in two SQLite files joined via `ATTACH DATABASE`:

- **`ledger.db`** (committed, ≤50 MB) — paper-trading record:
  positions, settlements, bankroll, equity, health, daily/scout
  reports, sharpline orders, logic violations, plus the lookup
  tables (markets, signals).
- **`cache.db`** (gitignored, rebuildable) — raw scan data:
  snapshots, *_gaps, cv_pairs, wallet trade history, odds cache,
  lp_sim estimates.

Migration tool `tools/migrate_split_db.py` proved zero-loss across
all 28 user tables + operational invariants (open positions, open
stake, bankroll allocations, txn count). Source-of-truth for the
LEDGER vs CACHE classification lives in that script.

All three share `concurrency.group: polymarketbot-state` so they
never race on the `git push` back to main.

## Test counts

- bucket_arb: 5
- arb_multi: 5
- cross_venue: 19 (14 original + 4 v2 confidence/category + 1 time-budget)
- cv_probe: 12 (v2 - quarantine, dedupe, caps, agreement outcomes, confidence gate)
- copy_trading: 10
- bankroll + health + master report: 8
- sharpline: 12 (8 original + 4 fill-lifecycle)
- lp_sim: 4
- logic_scan: 7
- fees: 14 (7 Polymarket + 7 Kalshi pinned-rate tests, v2 added)

**Total: 132 tests, all passing.**
