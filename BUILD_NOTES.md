# Build notes (V2 paper-only)

Status of every strategy shipped in V2. None are disabled.

## Active strategies

| # | name             | status | notes |
|---|------------------|--------|-------|
| 1 | weather          | ACTIVE | V1 carry-over; bankroll-wired. |
| 2 | bucket_arb       | ACTIVE | single-leg + multi-mode (Prompt A's `arb_multi` table). |
| 3 | cross_venue_arb  | ACTIVE | Polymarket vs Kalshi; FUZZY pairs never auto-traded. |
| 4 | copy_trading     | ACTIVE | Scout via /trades aggregation - leaderboard endpoint not reachable. |
| 5 | sharpline        | ACTIVE | OBSERVE MODE active when `ODDS_API_KEY` is empty/missing. |
| 6 | lp_sim           | ACTIVE | v1 = static reward score estimate; fills not simulated yet. |
| 7 | logic_scan       | ACTIVE | conservative same-event-family templates only. |

## Resilience-rule disables

None. Every phase reached fully green tests on the first attempt
without needing the disable-via-config fallback.

## Outstanding deferred work

- **Sharpline fill simulation**: order lifecycle columns exist
  (`filled_at`, `line_at_fill`, `adverse_selection`, `realized_pnl`)
  but the strict price-through fill check is not yet implemented.
  Currently a posted RESTING order stays at status='RESTING' until
  the maturity of the underlying market; the cycle that adds the
  fill-detection loop should add `cycle_simulate_fills(ledger,
  scanner)` and wire it into the existing `cycle` command.
- **Polymarket fee schedule**: shipped with `fee_taker_pct=0.0,
  fee_maker_pct=0.0` matching the current Polymarket protocol-fee
  state (zero). Re-verify against published docs before any phase
  treats fee math as load-bearing for trade decisions.
- **Copy-trading leaderboard fallback**: `/leaderboard` doesn't exist
  on data-api.polymarket.com - documented in `docs/api_notes.md` with
  the full list of 404s probed. Scout falls back to /trades
  aggregation + config seed_wallets. If/when a leaderboard endpoint
  appears, plug it into `CopyTrading.discover_candidates` as a third
  source.

## Test counts

- bucket_arb: 5 tests
- arb_multi (Prompt A): 5 tests
- cross_venue: 14 tests
- copy_trading: 10 tests
- bankroll + health + master report (Prompt B): 8 tests
- sharpline: 8 tests
- lp_sim: 4 tests
- logic_scan: 7 tests

**Total: 61 tests, all passing.**
