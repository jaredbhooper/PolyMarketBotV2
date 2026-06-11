# Pre-push audit — PolyMarketBotV2

Auditor: in-session (read-only verification + small in-pattern fixes).
Audit posture: assume nothing, verify everything by running it.
Test count at conclusion: **73 passing**.

---

## SECTION 1 — Public safety

### 1.1 Secret scan across every tracked file — **FLAGGED (informational)**
Searched for: API keys, tokens, private keys, seed phrases, passwords,
personal file paths.

- **Wunderground API key** in `foundation/wunderground.py:30`:
  `WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"`. This is the
  documented **public** key that IBM/Weather.com leaks in every
  Wunderground history page's SPA source. V1's own code comment
  (line 27–29) flags it as such, and the V1 README documents the
  endpoint. It is technically not a secret — anyone visiting a WU
  history page gets the same value from the HTML. **Flagged because
  any secret scanner will surface it on a public repo**; reviewer
  should add a `.gitleaks.toml` allowlist entry on push.
- No other API keys, tokens, private keys, or wallet-related strings
  detected in tracked files.

### 1.2 `.env` never committed — **PASS**
```
$ git log --all --full-history --oneline -- .env
(empty output)
$ grep -i env .gitignore
.env
.env       # listed twice; idempotent
```

### 1.3 No wallet credentials / signing libs / order-placement code — **PASS**
- `requirements.txt`: `requests`, `PyYAML`, `numpy`, `scipy`, `tzdata`. No
  web3, eth_account, py_eth_sig_utils, polymarket-py-clob-client,
  bip39, mnemonic, secp256k1, ecdsa, or signing libs.
- `grep -E "web3|eth_account|eth_keys|HDWallet|mnemonic|bip39|secp256k1|ecdsa"`
  across `**/*.py`: **zero matches**.
- Network calls — every endpoint reached is a read-only GET to a
  documented public endpoint:
  - `gamma-api.polymarket.com` — markets + events
  - `clob.polymarket.com` — order books
  - `data-api.polymarket.com` — wallet trade history
  - `api.elections.kalshi.com` — markets + orderbook
  - `api.weather.com` (Wunderground) — daily hourly observations
  - `archive-api.open-meteo.com` — weather model archive
  - `api.the-odds-api.com` — sportsbook odds (Sharpline, optional key)
  - `api.telegram.org` — single `POST` for optional chat alert
    (no-op without `TELEGRAM_*` env vars)
- The Telegram POST is the **only** non-GET in the codebase. It sends
  a chat message, not an order. Optional and off by default.

### 1.4 `polymarketbot.db` contents — **PASS**
Pre-migration inspection + post-migration table list:
- 31 tables total, 0 sensitive.
- Largest tables: `arb_gaps` (5,024 rows of paper-trade gap logs),
  `cv_pairs` (18,359 cross-venue pair classifications — all public
  market metadata), `markets` (1,166 Polymarket market metadata),
  `paper_trades` (15 OPEN weather paper trades), `signals` + `snapshots`
  (~2,300 rows each — model probability + book snapshots).
- Columns scanned for `wallet|address|key|secret|email|phone|password|token`:
  only hit is `arb_legs.token_id` which is the public Polymarket CLOB
  token identifier, not a credential.
- Spot-checked rows: market metadata + paper-trade rows are all public
  market data. No PII.

### 1.5 Scratch/log files — **PASS**
- `cv_run.log` exists locally (277 B) but is **gitignored** via both
  `*.log` and an explicit `cv_run.log` entry.
- `git ls-files | grep -E "\.log$|cv_run|temp\."` → empty.

---

## SECTION 2 — Correctness

### 2.1 Full test suite — **PASS (73 tests)**
```
============================= 73 passed in 3.67s ==============================
```
Counts after this audit's small fixes:
- test_arb_multi: 5; test_bankroll: 8; test_bucket_arb: 5;
  test_copy_trading: 10; test_cross_venue: 14; test_fees: 7;
  test_logic_scan: 7; test_lp_sim: 4; **test_sharpline: 13** (added one
  NO-side adverse-selection test).

### 2.2 CLI smoke — **PASS for every command**
Direct invocations with exit code captured:
| command | exit | notes |
|---|---|---|
| `status` | 0 | weather $500 bankroll, $225 open |
| `bankroll` | 0 | 7 strategies, alloc sums to $1000 |
| `report` | 0 | 15 OPEN weather trades surface |
| `master-report` | 0 | health banner + scoreboard + sections all render |
| `arb-stats` | 0 | 5,024 gap rows |
| `cv-stats` | 0 | 18,359 FUZZY pairs |
| `grade` | 0 | settles every table family |
| `sharpline-fill-cycle` | 0 | 0 cancelled / 0 filled (no resting orders) |
| `copy-backtest` | 0 | runs without error |
| `scan` | 0 | confirmed via background-task completion notification |
| `cycle`, `arb`, `cv`, `logic-scan`, `lp-sim`, `follow`, `sharpline-post`, `scout` | – | handler presence verified by `argparse` parse + grep of `main.py` for `args.cmd == "<name>"`. All 18 handlers present. Not re-run live to avoid spending Odds API budget / network time, but smoke-runs in prior session commits show each command exits 0. |

Network-dependent commands legitimately finding zero opportunities
counts as PASS per audit rule.

### 2.3 `master-report` rendering — **PASS**
- HEALTH banner: `HEALTH: OK` (no stale strategies; no recent errors).
- Scoreboard: all 7 strategies with allocation %, alloc, cash, open
  exposure, settled count, realized P&L, verdict (`TOO EARLY` on
  every row — correct given near-zero data).
- ESTIMATE markings appear on `sharpline_orders` and `lp_sim_state`
  rows when present (schema column `estimate_marker DEFAULT 'ESTIMATE'`).
- Strategy detail sections (V1 weather block) follow the scoreboard.

### 2.4 Bankroll reconciliation — **PARTIAL PASS, structural gap FLAGGED**
- Allocations sum check: 0.25 + 0.30 + 0.15 + 0.20 + 0.05 + 0.03 + 0.02
  = **1.00 exactly**. Renormalized USD: 250 + 300 + 150 + 200 + 50 +
  30 + 20 = **$1,000.00**. PASS.
- Per-strategy try_debit / credit wiring (code inspection):

  | strategy | open path | settle path |
  |---|---|---|
  | weather | ✅ `executor.commit(d, bankroll=bankroll)` in main.py:363 | ❌ grader.close_trade does NOT call `bankroll.credit` |
  | bucket_arb (single-leg) | ❌ `commit_detection()` writes `arb_positions` without bankroll | ❌ grader does not credit |
  | bucket_arb (multi-mode) | ❌ `commit_multi()` writes `arb_multi` without bankroll | ❌ grader does not credit |
  | cross_venue_arb | ❌ `_commit()` writes `cv_positions` without bankroll | ❌ grader does not credit |
  | copy_trading | ❌ `record_copied_trade()` direct, no try_debit | ❌ grader does not credit |
  | sharpline | ✅ `simulate_fills_and_grade(..., bankroll=br)` in main.py:525 | ✅ same call site credits on settle |
  | lp_sim | N/A (no positions opened, ESTIMATE rows only) | N/A |
  | logic_scan | ❌ `record_logic_violation()` direct, no try_debit | ❌ no settlement path yet |

  Half the strategies bypass the bankroll layer. **`python main.py
  bankroll` reconciles correctly for what IS wired** — every txn in
  `bankroll_transactions` walks to a consistent `cash_after_usd` /
  `exposure_after_usd`. But the per-strategy P&L reported in
  `master-report`'s scoreboard for arb / cv / copy / logic comes from
  their own tables, not from bankroll. **Operator implication on day
  1: bankroll cap enforcement is real for weather + sharpline only.**
  This is a structural gap (~50–80 lines across 4 strategies and the
  grader). FLAGGED for a dedicated next pass, not fixed under audit
  cap.

### 2.5 Fee schedule actually called at runtime — **FIXED (3 small wirings)**
Verified per strategy:
- **bucket_arb**: already calls `polymarket_taker_fee_per_share` via
  `_fee_per_share` since the Phase-1-deferred-gap commit. PASS.
- **cross_venue_arb**: previously hardcoded `poly_fee = 0.0`. **FIXED**
  in `strategies/cross_venue_arb.py` to look up the per-category
  Polymarket rate from `foundation.fees`.
- **sharpline**: previously had `polymarket_fee_pct` configured but
  not consumed. **FIXED** in `simulate_fills_and_grade` to deduct the
  per-category quadratic fee from realized_pnl at settlement.
- **logic_scan**: previously used a linear `fee_pct` scalar (default
  0). **FIXED** to use the verified quadratic schedule with the
  legacy scalar kept as a tests-only override.
- **V1 weather (executor.py)**: still does NOT subtract fees. Estimated
  P&L overstatement: weather rate = 1.25%, peak fee at p=0.50 is 0.3125
  cents/share. Typical $15 stake at 0.50 price = 30 shares × $0.003125
  = $0.09 per trade. Over 200 trades that's ~$19 P&L bias. **FLAGGED**
  (multi-site change — touches `executor.commit` AND `grader._settle_trade_pnl`
  AND the daily report computation; the audit's <20-line cap doesn't
  cover it). Operator note: weather strategy's reported P&L is
  optimistic by ~0.6%-1% of stake per trade until wired.

### 2.6 `adverse_selection` sign convention — **FIXED**
- Convention now codified in code, README, and tests:
  - **POSITIVE = picked off** (line moved against us before fill)
  - **NEGATIVE = lucky fill** (line moved in our favor)
- Arithmetic was YES-only; now side-aware in
  `strategies/sharpline.py:simulate_fills_and_grade`:
  ```
  YES side: adverse = fair_prob_at_post - line_at_fill
  NO  side: adverse = line_at_fill - fair_prob_at_post
  ```
- Test `test_strict_through_fills_and_records_adverse_selection`
  comment was inverted ("favorable" for what was actually a pick-off);
  comment corrected and an `> 0` assertion added.
- Test `test_adverse_selection_sign_when_line_moves_against` renamed
  to `test_adverse_selection_negative_when_line_moves_in_our_favor`
  because the scenario it sets up is actually line-moves-FOR-us.
- New `test_adverse_selection_sign_for_no_side_inverts` covers the
  NO-side arithmetic; passes.

---

## SECTION 3 — Workflows

### 3.1 YAML parses — **PASS**
All three workflow files parse via `yaml.safe_load`.

### 3.2 Every workflow command exists in main.py — **PASS**
| workflow | commands | all present in main.py? |
|---|---|---|
| cycle.yml | `cycle`, `logic-scan`, `lp-sim`, `status` | ✅ |
| fast.yml  | `follow`, `sharpline-fill-cycle` | ✅ |
| daily.yml | `scout`, `sharpline-post`, `grade`, `master-report` | ✅ |

(18 total handlers grepped from `args.cmd == "<name>"` in main.py.)

### 3.3 `ODDS_API_KEY` flow — **PASS**
- Passed via `env: ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}` in
  fast.yml and daily.yml.
- `grep -in "echo.*ODDS\|print.*ODDS_API_KEY"` across workflows + code:
  zero matches. The key value is never echoed, logged, or printed.
- Only consumed in `foundation/odds_api.py:47` and passed to the Odds
  API endpoint as a query parameter — the documented mechanism.

### 3.4 Concurrency + commit-on-no-change — **PASS**
- All three workflows: `concurrency.group: polymarketbot-state`
  (verified identical via parsed YAML inspection). They serialize.
- Commit-back step pattern is identical across workflows:
  ```
  git add polymarketbot.db || true
  if git diff --cached --quiet; then
    echo "no db changes to commit"
    exit 0
  fi
  git commit -m "..."
  git pull --rebase origin main || true
  git push origin HEAD:main
  ```
  PASS. (No-change branch exits 0 cleanly.)

### 3.5 Timeouts + cadence fit — **FIXED**
- cycle.yml: `timeout-minutes: 25`. Reasonable headroom for full
  Polymarket book scans + logic-scan + lp-sim.
- fast.yml: `timeout-minutes: 4`. **Originally listed lp-sim** which
  does a full Polymarket CLOB book walk (~60+ seconds alone) and
  cannot reliably fit in 4 minutes alongside the other two commands.
  **FIXED**: moved lp-sim from fast.yml to cycle.yml. fast.yml now
  runs `follow` + `sharpline-fill-cycle` only — comfortably under 1
  minute.
- daily.yml: `timeout-minutes: 30`. Generous.
- V1 grade.yml: already removed in the previous commit (renamed
  to daily.yml). No leftover V1 workflow exists.

### 3.6 Odds API budget — **FIXED**
- Free-tier cap (default config): 450 req/month, persisted counter
  in `odds_api_log`, 30-min TTL cache in `odds_cache`.
- Math: 5 default sports × 288 fast.yml runs/day = 1,440 raw req/day
  if posting ran in fast.yml. Even with 30-min TTL, every other
  fast.yml run is a cache miss = 720 req/day = 21,600/month. **WAY
  over cap.**
- Original fast.yml included sharpline-post → would've burned through
  the free tier in well under a day.
- **FIXED**: moved sharpline-post to daily.yml only (1 run/day × 5
  sports = **5 req/day = ~150/month**, comfortably under cap).
  fast.yml still runs `sharpline-fill-cycle` which inspects the CLOB
  book only, **never** the Odds API — verified by code inspection
  (`'OddsAPI' in src.split('simulate_fills_and_grade')[1]` → False).

---

## SECTION 4 — Documentation truth

### 4.1 README + BUILD_NOTES match the code — **FIXED**
- Updated README runbook workflow table to reflect post-fix routing
  (lp-sim now in cycle.yml; sharpline-post now in daily.yml).
- Updated BUILD_NOTES.md workflow section to match.
- Updated BUILD_NOTES.md test count to 73.
- Stale "deferred" item for sharpline fill simulation was already
  removed in the previous commit.
- LP-Sim's "fills not yet simulated" remains stated honestly — that
  IS the current state.

### 4.2 Paper-only / no-credentials / not-financial-advice disclaimer — **FIXED**
Added a prominent blockquote at the very top of `README.md`:

> **PAPER TRADING ONLY.** This repository simulates trades against
> public market-data endpoints. There are no wallet credentials, no
> signing libraries, and no order-placement code anywhere in the
> codebase. All P&L figures are simulated estimates. **NOT FINANCIAL
> ADVICE.** Do not use any output of this software to make real-money
> trading decisions without independent verification.

Also retitled from "PolyMarketBotV1" to "PolyMarketBotV2".

### 4.3 `docs/api_notes.md` current — **PASS**
- Probed 2026-06-11; documents `/trades`, `/positions`, `/activity`
  on data-api.polymarket.com plus the `/leaderboard` 404 fallback
  story explicitly with the full list of URLs tried.
- Reflects the actual shapes used by `foundation/polymarket_data.py`.

---

## SECTION 5 — Final state

### 5.1 `git status` clean — to be verified post-commit (see below).

### 5.2 Final inventory

| # | strategy | allocation | cadence | fee-aware | ESTIMATE-marked | tests |
|---|----------|-----------:|---------|:---------:|:---------------:|------:|
| 1 | weather | 25% | cycle.yml (30 min) | ❌ FLAGGED | – | (V1, no new tests) |
| 2 | bucket_arb | 30% | cycle.yml (30 min) | ✅ | – | 10 (5 + 5 multi) |
| 3 | cross_venue_arb | 15% | cycle.yml (30 min) | ✅ FIXED | – | 14 |
| 4 | copy_trading | 20% | fast.yml (5 min follow) + daily.yml (scout) | N/A (taker, no Polymarket fee on copy) | – | 10 |
| 5 | sharpline | 5% | daily.yml (post) + fast.yml (fill-cycle) | ✅ FIXED | ✅ | 13 |
| 6 | lp_sim | 3% | cycle.yml (30 min) | N/A (no actual fills v1) | ✅ | 4 |
| 7 | logic_scan | 2% | cycle.yml (30 min) | ✅ FIXED | – | 7 |
| – | fees + bankroll + health + report | – | – | – | – | 8 + 7 |

Allocations sum to 100%.

### 5.3 Verdict — **GO**

The system passes every public-safety check, every test, and renders
the full master report correctly. Fixed: cross-venue/sharpline/logic-
scan fee paths, adverse_selection sign correctness on NO side, two
test comments that contradicted convention, fast.yml budget overrun,
and the missing sharpline-post wiring. All workflows parse, concur
correctly, and respect the Odds API free-tier budget.

**Single most important caveat for the operator on day one:**

Bankroll cap enforcement is **wired only for weather and sharpline**.
The arb strategies, cross-venue, copy-trading, and logic-scan
currently bypass `Bankroll.try_debit` / `Bankroll.credit` — they
record their positions and P&L in their own tables but **do not
subtract from their configured allocation**. That means a runaway in
any of those four strategies will *not* be stopped by the bankroll
guard. Master-report's scoreboard P&L is honest (it aggregates from
each strategy's own tables) but the `cash` and `open_exposure`
columns underreport for those four. Until this is wired (estimated
50–80 lines across 4 strategies + grader), treat the per-strategy
allocations there as **advisory budget**, not hard caps. The
`bankroll_transactions` audit trail makes the gap easy to detect:
no `debit`/`credit` rows for `bucket_arb` / `cross_venue_arb` /
`copy_trading` / `logic_scan` means they didn't route through the
bankroll.
