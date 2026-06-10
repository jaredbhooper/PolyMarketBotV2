# Build notes (V2 paper-only)

Status of every strategy shipped in V2. None are disabled.

## Active strategies

| # | name             | status | notes |
|---|------------------|--------|-------|
| 1 | weather          | ACTIVE | V1 carry-over; bankroll-wired. |
| 2 | bucket_arb       | ACTIVE | single-leg + multi-mode (`arb_multi` table). |
| 3 | cross_venue_arb  | ACTIVE | Polymarket vs Kalshi; FUZZY pairs never auto-traded. |
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

- `cycle.yml` — `*/30 * * * *`. Runs `python main.py cycle` + logic-scan +
  status. Commits `polymarketbot.db` back to the branch.
- `fast.yml` — `*/5 * * * *`. Runs `follow`, `sharpline-fill-cycle`, and
  `lp-sim`. Each command is idempotent and finishes within the
  workflow's 4-minute timeout. `ODDS_API_KEY` is read from repo
  secrets; without it Sharpline runs in OBSERVE MODE.
- `daily.yml` — `0 10 * * *`. Runs `scout`, `grade`, and
  `master-report`.

All three share `concurrency.group: polymarketbot-state` so they
never race on the `git push` back to main.

## Test counts

- bucket_arb: 5
- arb_multi: 5
- cross_venue: 14
- copy_trading: 10
- bankroll + health + master report: 8
- sharpline: 12 (8 original + 4 fill-lifecycle)
- lp_sim: 4
- logic_scan: 7
- fees (verified schedule): 7

**Total: 72 tests, all passing.**
