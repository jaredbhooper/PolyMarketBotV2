# PolyMarketBotV2

> **PAPER TRADING ONLY.** This repository simulates trades against public
> market-data endpoints. There are no wallet credentials, no signing
> libraries, and no order-placement code anywhere in the codebase. All
> P&L figures are simulated estimates. **NOT FINANCIAL ADVICE.** Do not
> use any output of this software to make real-money trading decisions
> without independent verification.

Paper-trading rig for Polymarket (and Kalshi, via the cross-venue
strategy). A strategy-agnostic foundation (scanner → edge engine →
paper executor → SQLite ledger → grader → reporter) wraps a pluggable
strategy interface. Seven strategies ship with V2 — see "Inventory"
below.

Zero spend in paper mode — every primary API used (Polymarket Gamma,
Polymarket CLOB, Polymarket data-api, Kalshi, Open-Meteo,
Wunderground) is free and authenticationless. Sharpline optionally
reads a free-tier Odds API key from `ODDS_API_KEY` (env or `.env`)
and falls back to OBSERVE MODE without one.

## Repo layout

```
PolyMarketBotV2/
├── main.py                # entrypoints: cycle, scan, grade, report, status
├── config.yaml            # active strategies + 5 cities + paper params
├── foundation/            # strategy-agnostic, permanent
│   ├── scanner.py         #   Gamma API + CLOB book → Market objects
│   ├── edge.py            #   YES/NO edge helpers
│   ├── executor.py        #   skip-rules + book-walking paper fills
│   ├── ledger.py          #   SQLite (markets, snapshots, signals, paper_trades, settlements, daily_report)
│   ├── grader.py          #   settles markets; computes Brier + P&L per strategy
│   └── report.py          #   console summary; optional Telegram hook
├── foundation/
│   ├── equivalence.py    #   cross-venue rules-equivalence engine (strategy #3)
│   └── venues/           #   venue adapters
│       ├── base.py       #     VenueMarket dataclass + Venue ABC
│       ├── polymarket.py #     wraps the existing Scanner
│       └── kalshi.py     #     Kalshi public market-data API
└── strategies/            # swappable plug-ins
    ├── base.py            #   Strategy ABC + Estimate + ArbEvent / ArbLeg
    ├── dummy.py           #   bench strategy for testing the foundation
    ├── weather.py         #   Strategy #1: ensemble temperature model
    ├── bucket_arb.py      #   Strategy #2: bucket-sum arbitrage detector
    └── cross_venue_arb.py #   Strategy #3: cross-venue arb (Polymarket x Kalshi)
```

## Setup

```bash
python -m pip install -r requirements.txt
```

Requires Python 3.11+. On Windows the `tzdata` package is needed because
`zoneinfo` has no IANA data shipped with the standard library.

## Running a cycle manually

```bash
# Live scan + log signals + paper-trade (writes polymarketbot.db).
python main.py cycle

# Just print the scanner table for the 5 cities (no DB writes).
python main.py scan

# Settle resolved markets, score Brier + P&L.
python main.py grade

# Print latest report and per-strategy bankroll.
python main.py report
python main.py status

# Strategy #2: bucket-sum arb only (no weather scan).
python main.py arb            # full event-level cycle + diagnostics
python main.py arb-stats      # print gap-distribution diagnostics without rescanning

# Strategy #3: cross-venue (Polymarket x Kalshi) arb only.
python main.py cv             # match + classify pairs, log gaps, paper-fire CERTIFIED
python main.py cv-stats       # print pair + gap diagnostics without rescanning
```

A `cycle` does, per strategy:

1. Pull all markets under the `highest-temperature` tag from Polymarket
   Gamma, build full order books from the CLOB for both YES and NO tokens.
2. Each strategy filters down via `relevant_markets()` and emits an
   `Estimate(p_final, confidence, metadata)` per market.
3. The foundation logs a snapshot + signal for every relevant market
   regardless of trade (sec 7 of the plan — bias correction needs no-trade
   data too).
4. If `edge ≥ threshold`, market has ≥ 2h to resolve, spread ≤ 6c, and
   cumulative ask depth within 5c of the best ask ≥ $50, size with
   fractional Kelly × confidence (cap $15/market, cap 15 open positions),
   then **walk the actual order book level-by-level** and record a fill at
   VWAP + 1c slippage. Levels consumed are stored as JSON on the trade row.

`grade` walks every OPEN trade whose market has resolved, asks the strategy
that opened it for an outcome (via `Strategy.resolve()`), settles the P&L,
and updates the daily report row. The weather strategy resolves via the
Open-Meteo archive API at the station's exact lat/lon. (See "Resolution
source caveat" below.)

## GitHub Actions scheduler

Two workflows in `.github/workflows/`:

- `cycle.yml` — `cron: */30 * * * *` runs `python main.py cycle`.
- `grade.yml` — `cron: 0 10 * * *` runs `python main.py grade` then
  `python main.py report`.

Both commit `polymarketbot.db` back to the repo so state survives across
runs. Free-tier cron drift is 5–15 min — irrelevant at our timescale.

### To enable

1. Push this folder to a **private** GitHub repo (the DB will be committed
   in plaintext — it contains no secrets, but you probably want it private
   anyway).
2. Settings → Actions → General → **Workflow permissions: Read and write**.
   (The default "Read repository contents" cannot push the DB back.)
3. Cron starts firing automatically. Trigger a first run manually from the
   **Actions** tab to verify (each workflow has `workflow_dispatch`).

### Optional Telegram alerts

The reporter is wired for a Telegram bot but is silent unless two secrets
are set in **Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN` — from `@BotFather`
- `TELEGRAM_CHAT_ID` — your chat ID (use `@userinfobot`)

No alerting needed for the bot to run; this is decoration.

### Don't want the DB committed?

Swap the "Commit ledger" step in each workflow for `actions/upload-artifact`.
You lose cross-run state but the ledger is rebuilt every cycle from
Polymarket anyway (open positions need persistence; signals/snapshots do not).

## Adding a new strategy

Three steps. The foundation needs no changes.

**1. Create `strategies/<your_name>.py`** with a class subclassing the
Strategy ABC and a `build(cfg)` factory:

```python
from strategies.base import Estimate, Market, Strategy

class MyStrategy(Strategy):
    name = "my_strategy"               # used as primary key everywhere

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.edge_threshold  = float(s.get("edge_threshold", 0.08))
        self.kelly_fraction  = float(s.get("kelly_fraction", 0.15))
        # ... pull your own params out of cfg ...

    def relevant_markets(self, markets):
        # Filter the scanner's full universe down to ones you can price.
        return [m for m in markets if "...my filter..." in m.slug]

    def estimate(self, market: Market) -> Estimate | None:
        p = ...                                # compute your probability
        return Estimate(
            p_final=max(0.02, min(0.98, p)),   # clamp inside [0.02, 0.98]
            confidence=1.0,
            metadata={"any": "json-able dict", "members": [...]}
        )

    # Optional: tell the grader how to settle YOUR markets. Return
    # {"outcome": "YES"|"NO"|"VOID", "actual_value": float|None,
    #  "source_value": "human-readable note"} or None to leave open.
    def resolve(self, market: Market, settled_at: str) -> dict | None:
        ...

def build(cfg: dict) -> Strategy:
    return MyStrategy(cfg)
```

**2. List it in `config.yaml`:**

```yaml
active_strategies:
  - module: weather
  - module: my_strategy          # add this line

strategies:
  my_strategy:
    edge_threshold: 0.10         # any per-strategy params
    kelly_fraction: 0.10
    ...
```

**3. Run a cycle.** The foundation will discover your strategy, route the
markets you say are relevant to it, log signals + paper trades under the
`my_strategy` name, and track its own Brier + P&L row in `daily_report`.

The Strategy contract — strategies **never** touch prices, the executor, or
the ledger directly. They only price probability. If you find yourself
wanting to skip the edge engine ("this strategy needs to fill at the
midpoint!"), don't — change the foundation rules and accept they apply to
every strategy.

## Configuration (`config.yaml`)

- `paper.*` — bankroll, edge threshold, Kelly fraction, slippage, depth
  rules. These are foundation-wide defaults; any strategy can override
  `edge_threshold`/`kelly_fraction` in its own block.
- `active_strategies` — list of module names. The order isn't meaningful;
  each runs independently.
- `strategies.<name>.cities` — for `weather`, the 5 stations (lat/lon,
  timezone, resolution source). **Forecast the station's exact coordinates,
  not the city center.** Heathrow ≠ central London by 1–2 °C on some days.

## Resolution sources (audited against live Gamma `description` text on 2026-06-10)

Polymarket weather markets resolve against the **Weather Underground**
"History" page for a specific airport station. The station is named in the
market's description text, and *it is not always the one you'd guess from
the city name*. Across every open weather event I sampled, each city has
exactly one station — the audit pass:

| City     | Station                          | ICAO | Wunderground URL |
|----------|----------------------------------|------|------------------|
| London   | **London City Airport**          | EGLC | `/history/daily/gb/london/EGLC` |
| Paris    | **Paris-Le Bourget**             | LFPB | `/history/daily/fr/bonneuil-en-france/LFPB` |
| New York | **LaGuardia**                    | KLGA | `/history/daily/us/ny/new-york-city/KLGA` |
| Miami    | Miami Intl                       | KMIA | `/history/daily/us/fl/miami/KMIA` |
| Chicago  | Chicago O'Hare                   | KORD | `/history/daily/us/il/chicago/KORD` |

These are the coords baked into `config.yaml`. Three were wrong in my first
draft (London was Heathrow, Paris was Orly, NYC was Central Park) — the
station mismatch alone shifted June 9 London's daily-max by ~0.5 °C, which
is enough to flip 1-in-3 boundary fills. **Verify before adding a new city.**

If Polymarket ever introduces a city whose station varies *across dates*,
the scanner already stashes the per-market URL in `market.extras['station_url']`
and the per-market `description` in the trade's `rules_text` column — wire
those into the resolve step instead of the city-default. For now, single
station per city.

The grader pulls **both** sources every settlement:

1. **Open-Meteo archive** at the station coords (model reanalysis).
2. **Wunderground** — direct station observation, via `api.weather.com`
   (the same endpoint the WU history page calls; the API key is published
   in the page source). `foundation/wunderground.py` filters hourly obs
   to the market's local date and returns the max.

Both values land in the `settlements` row as `actual_value` (OM) and
`wu_value` (WU). They're stored in the market's display unit (°C or °F)
so the integers can be compared directly.

### Dispute handling

If `|round(om) - round(wu)| >= 1` after round-half-up (configurable via
`strategies.weather.dispute_threshold_degrees`):

- Settlement outcome is set to `DISPUTED`, not `YES`/`NO`.
- The trade stays `OPEN` — it is NOT closed.
- The daily report surfaces the dispute with both temps and a link to
  the WU history page.

To resolve: read the WU page, decide which value is right, then either
`UPDATE settlements SET outcome='YES'|'NO'|'VOID' WHERE market_id=...` by
hand or just wait until the next morning when WU has finalized the
observation (the archive sometimes catches up to WU within 24h). Re-run
`python main.py grade` to close the trades using the corrected outcome.

When WU and OM agree, the grader uses **WU's rounded integer** as the
verdict (since WU is the actual resolution source). When WU is unavailable
(rate limit, station outage, API key rotation), the grader falls back to
OM and the settlement source note records that.

## Bucket rounding convention

Every market description says: *"measures temperatures to whole degrees
(eg, 9°C). Thus, this is the level of precision that will be used when
resolving the market."* The natural reading — and the one this codebase
uses — is **round-half-up to a whole integer, then resolve to the bucket
that contains that integer**:

- `"16°C"` bucket wins iff `round(actual_C) == 16`, i.e. `actual_C ∈ [15.5, 16.5)`
- `"13°C or below"` wins iff `round(actual_C) ≤ 13`, i.e. `actual_C < 13.5`
- `"23°C or higher"` wins iff `round(actual_C) ≥ 23`, i.e. `actual_C ≥ 22.5`
- Miami `"86-87°F"` bucket wins iff `round(actual_F) ∈ {86, 87}`, i.e. `actual_F ∈ [85.5, 87.5)`

The probability model and the grader use the same boundaries. The
probability uses `P(actual_C ∈ [t-0.5, t+0.5))` (Gaussian CDF with
continuity correction, blended with the raw ensemble fraction). The grader
uses `math.floor(actual + 0.5)` — both are round-half-up. If Wunderground
turns out to use round-half-to-even (banker's rounding) instead, half-degree
boundary days will misgrade by 1 bucket — flag and switch.

## Multi-family ensemble (current model bundle)

Open-Meteo serves 4 model families through one Ensemble API call. We pull
all of them per cycle and pool every member into the probability:

| Family | Models string | Members | Notes |
|---|---|---|---|
| GFS (NCEP) | `gfs_seamless` | 31 | GEFS dynamical ensemble |
| ECMWF IFS | `ecmwf_ifs025` | 51 | The skill leader on most stations |
| ECMWF AIFS | `ecmwf_aifs025` | 51 | ECMWF's AI model ensemble (note: use the bare slug, **not** `_single` — the latter returns the deterministic forecast which is currently empty through the ensemble endpoint) |
| ICON (DWD) | `icon_seamless` | 40 | Independent dynamical core |
| **Total** | | **173** | (was 82 before this bundle) |

The pooled estimate uses every member equally. The disagreement gate has
been generalized: it now flags when **any pair of model families** with
≥ `min_family_members_for_gate` (default 10) members disagree by more
than `disagreement_threshold` (default 0.25) on the bucket's probability.
Any family with fewer than that many members (e.g., a deterministic
forecast) still pools into the main estimate but is excluded from the
gate (single-member P is binary and uninformative). The signal metadata
stores `family_n`, `family_p`, and a `disagreement_pair` string
identifying the two families driving the max-spread for debugging.

## Asymmetric edge threshold (longshot YES protection)

NO-side trades clear the default 0.08 edge threshold. **YES-side trades
on buckets whose ask is under `longshot_yes_ask_cap` (default 0.15) must
clear `longshot_yes_edge_threshold` (default 0.15) instead.** Rationale:

At a 5c YES ask, a 2c error in our model P is a 40% relative miscalibration
of the price. The same 2c error at a 50c ask is 4% relative. Symmetric
edge thresholds let longshot YES bets sneak through with cheap-looking
edges that are actually within model-noise. The asymmetric rule is a
foundation-level setting in `paper:` and applies to every strategy.

The rule is checked twice:

1. **Pre-fill**: candidates are scored against their own threshold. The
   best **passing** side wins — not just the side with the highest raw
   edge (a longshot YES with edge 0.13 doesn't beat a non-longshot NO
   with edge 0.09).
2. **Post-fill**: after walking the book and adding 1c slippage, the
   effective edge is re-checked against the same threshold. A 0.15 YES
   bet that walks to 0.10 effective edge gets `NO_EDGE_POST_FILL`.

Override either knob in `config.yaml` under `paper:`. Setting
`longshot_yes_edge_threshold` equal to `default_edge_threshold` disables
the asymmetry.

## Autonomous decisions I made building this (read me)

I had to make a handful of judgment calls. Override any of these in
`config.yaml` if you disagree:

- **Depth check semantics.** The plan says "SKIP if book depth at ask < $50".
  Top-of-book depth alone is often a few dollars even on liquid markets;
  the rule then rejects almost everything. I interpreted "depth at ask" as
  "cumulative USD of asks within 5c of the best ask" — the band you'd
  actually walk to honestly absorb a $50 order. This is in
  `foundation/executor.py::_depth_within`. Set `paper.min_book_depth_usd`
  lower if you want more fills.
- **YES/NO depth source.** The scanner fetches the CLOB book for both
  tokens. Gamma's `bestAsk` lags the live book by minutes — we always use
  the live CLOB ask, not Gamma's snapshot.
- **Probability "bucket" math.** Polymarket weather markets resolve to the
  station's daily max **rounded to a whole integer**. So "14°C" means
  `round(actual) == 14`, i.e. `actual ∈ [13.5, 14.5)`. The probability uses
  a continuity-corrected boundary at `t ± 0.5`; the grader uses
  `math.floor(actual + 0.5)`. Miami's 2°F buckets work the same way
  ("86-87°F" ⇒ `actual ∈ [85.5, 87.5)`). See "Bucket rounding convention"
  above.
- **Single bankroll, per-strategy P&L.** All strategies share the
  `$1000` paper bankroll cap (max 6 open positions across the combined
  book), but Brier and P&L are scored separately. This matches sec 6 of
  the plan and lets one strategy stand or fall on its own.
- **"One position per market per day" key.** Implemented as
  `(market_id, strategy, UTC date)`. Two strategies can both open
  positions in the same market the same day; the foundation just won't let
  one strategy double up.
- **Resolution station vs forecast station.** Audited the live Gamma
  description text for each city and updated `config.yaml` to the actual
  resolution stations: London City Airport (EGLC), Paris-Le Bourget (LFPB),
  LaGuardia (KLGA), Miami Intl (KMIA), Chicago O'Hare (KORD). See
  "Resolution sources" above. Three of those were not the default for the
  city — debug station coords before model logic if Brier drifts.
- **Disagreement gate.** The build plan says drop confidence to 0.5 OR
  return None when GFS/ECMWF disagree by > 0.25. I picked confidence 0.5
  so the trade still books with a half-stake — better data on the
  disagreement zone than abstaining entirely.

## Notes on the SQLite schema (sec 7)

- `signals` carries `metadata_json` so per-strategy state (member maxima,
  µ, σ, p_raw, p_gauss, p_gfs, p_ecmwf, disagreement) lives in the same
  row without schema migrations when a new strategy lands.
- `paper_trades.levels_consumed_json` records the actual book walk —
  useful when you want to debug a fill against the historical snapshot.
- `daily_report` has `(date, strategy)` as the PK — one row per strategy
  per day.

## Strategy #2: Bucket-sum arbitrage detector

A multi-outcome (Polymarket negRisk) event has exactly one outcome that
resolves YES and pays $1 per share. Two free-money arbs follow directly:

- **YES-side**: if `Σ(YES ask_i) < $1.00`, buying one share of YES on every
  leg locks `$1 - Σ` per share. Whichever leg wins pays the $1; the rest
  are losing $0 lottery tickets that cost ~nothing because their ask was
  tiny.
- **NO-side (mirror)**: if `Σ(NO ask_i) < $(N-1)`, buying one share of NO
  on every leg locks `$(N-1) - Σ` per share. In a MECE set of N outcomes
  exactly N-1 NOs resolve true and each pays $1.

`strategies/bucket_arb.py` implements both. Built and trusted in two
layers:

1. **DETECTOR** (always logs, regardless of profit size).
   For every Polymarket event:
   - **Step 1 — Verify completeness.** A market set is MECE iff
     `event.negRisk == True` AND every leg has a deployed CLOB token
     (`clobTokenIds` populated) AND every leg is `active=True, closed=False`.
     This is Polymarket's own assertion of mutual exclusion + collective
     exhaustion — without that flag we *cannot* confirm the set is
     complete and we **skip with a `completeness_note`** rather than risk
     a fake arb signal.
   - **Step 2 — Cheap pre-filter.** Sum the Gamma snapshot `bestAsk`
     across legs (and implied NO ask = `1 - bestBid`). If neither sum is
     within `walk_band` (default 10c) of the arb threshold, log a
     `walk_mode='gamma_only'` row and move on. This is the cost-control
     gate — there are ~30k legs to walk at full depth and most are
     nowhere near arb.
   - **Step 3 — Book-walk every leg** for `target_shares` (default 100
     per leg). Cap the executable share count at `min(per-leg fill)` so
     the implied position is actually fillable across the whole set.
   - **Step 4 — Compute true locked profit** = `payout - Σ(VWAP_i) -
     N × slippage - safety_buffer`. Log every row to `arb_gaps` with
     the per-leg VWAPs, depth, and consumed levels.

2. **PAPER EXECUTOR** (only fires above `min_arb_profit`).
   For each detection whose `locked_profit_usd ≥ min_arb_profit`, open
   one `arb_positions` row covering all legs and N `arb_legs` rows
   (one per outcome). All legs settle together when the event resolves.

### Why the negRisk gate is non-negotiable

A non-negRisk multi-market event (e.g. "X happens by Aug 31", "Y happens
by Aug 31") can have multiple YES winners (or none). If we summed those
asks and saw $0.80, we'd buy YES on every leg, then watch one leg pay
$0 while three pay $1 — or all four pay $0 — without any guaranteed
$1 payout. The detector **must** refuse to trade those, and the
`completeness_verified` column on `arb_gaps` records every refusal.

### Resolution-source caveat

Even with negRisk-verified completeness, the *grader* needs every leg's
on-chain settlement to compute P&L. If Polymarket VOIDs a leg or pauses
resolution, the position stays OPEN. The grader checks each leg via
`/markets?condition_ids=` and only closes once all N legs have a final
`outcomePrices` set. Two-winners (MECE violation) → VOID + refund cost.

### Fees, slippage, and safety buffer

The locked-profit math is **strictly pessimistic**:

- Walk the actual order book level-by-level (never midpoint).
- Add `slippage_cents` (default 1c) per leg on top of VWAP — that's
  `N × 1c` for an N-leg event. Weather events (N=11) lose 11c per share
  to slippage alone, so even cheap-looking gaps usually evaporate.
- Subtract `safety_buffer` (default 0.5c) per share as a final cushion
  for Polymarket's per-trade fee + spread risk between scan and fill.
- `min_arb_profit` ($1.00 default) is the *USD* threshold to fire —
  a 1c-per-share gap on a 100-share fill is exactly $1, which is the
  break-even point for the paper executor.

Any of these can be overridden in `config.yaml` under `strategies.bucket_arb`.

### Schema additions

Three new SQLite tables (zero impact on `paper_trades` / weather):

- `arb_gaps` — one row per detected gap. **Every** gap is logged,
  including the gamma-only no-arb majority, with `walk_mode` distinguishing
  cheap snapshots from full-book walks. Columns include the per-leg
  VWAPs, executable share count, locked profit per share + USD, side
  (YES/NO), completeness verification flag, and a `cleared_threshold`
  bit.
- `arb_positions` — one row per paper-traded complete-set arb. Carries
  event metadata, side, common share count, total cost, expected payout,
  locked profit at entry, and status (`OPEN`/`CLOSED`/`VOID`).
- `arb_legs` — N rows per `arb_position`, each with the per-leg fill
  details (VWAP, slipped price, shares, cost, consumed levels, eventual
  outcome + realized payout).

### Config knobs

```yaml
strategies:
  bucket_arb:
    safety_buffer: 0.005           # USD per share, on top of slippage
    min_arb_profit: 1.00           # USD - paper-fire threshold
    target_shares: 100.0           # per-leg share target (capped by depth)
    walk_band: 0.10                # pre-filter: walk if gamma sum within this of arb
    slippage_cents: 0.01           # per-leg adverse slippage on VWAP
    detect_yes: true               # scan YES-side gaps
    detect_no: true                # scan NO-side gaps
    execute_yes: true              # paper-fire above threshold
    execute_no: true
    min_hours_to_resolve: 1.0      # skip events resolving in less than this
    min_executable_shares: 5.0     # require this many fillable shares to log full-walk
    max_walks_per_cycle: 2000      # hard cap on expensive CLOB book-walks
```

### Live scan result (2026-06-10 14:46Z)

One full pass against Polymarket's live event universe:

| metric                                              | value |
|-----------------------------------------------------|------:|
| events scanned (Gamma open+active universe)         | 8,767 |
| MECE-verified complete sets (negRisk + all deployed)|  1,173 |
| events failed completeness (skipped — flagged)      |  7,594 |
| events full-book walked (passed pre-filter)         |    352 |
| events recorded gamma-only summary (no arb possible)|    648 |
| events skipped (resolving in < 1h)                  |    173 |
| events skipped due to walk cap                      |      0 |
| total gap rows logged (`arb_gaps`)                  |  1,664 |
| full-book detections (real per-share profit)        |    543 |
| gaps that cleared `min_arb_profit = $1.00`          |  **0** |
| paper arb positions opened                          |      0 |

Per-share locked-profit distribution (full-book walks only):

| bucket          | count |
|-----------------|------:|
| `< -10c`        |  274 |
| `[-10c, -5c)`   |  189 |
| `[-5c, -2c)`    |   80 |
| `[-2c,  0)`     |    0 |
| `[0, +5c)`      |    0 |
| `≥ +5c`         |    0 |

**Read of the result.** The bucket-sum arb shows up *visually* — 7 of
2,128 fully-deployed negRisk events had `Σ(YES bestAsk) < $1.00` on the
Gamma snapshot (cheap-pass survey). But once you walk the actual book
for a tradeable share count, every leg's thin top-of-book gets eaten
fast and the realized VWAP climbs above the snapshot price. The
remaining `N × 1c` slippage + 0.5c buffer then pushes the per-share
profit firmly negative on every single complete set that exists right
now. Worst gap: -14c per share on a Brazilian soccer halftime market
(thin books, 30+ legs amplifying slippage). The detector worked
correctly — the universe simply isn't offering free money today.

This is exactly the result we wanted to measure: **how often a
fillable, profitable complete set actually appears at our scan
cadence.** The honest answer this hour is *zero out of 1,173*. Re-run
the scan periodically and watch the distribution shift — if it ever
sneaks above 0, the paper executor fires automatically.

### Manual run

```bash
python main.py arb            # one full scan: detect, log every gap, paper-execute clears
python main.py arb-stats      # diagnostics without rescanning (cheap)
```

The `arb` command runs *only* the bucket-sum detector; `python main.py
cycle` runs both weather and arb in the same pass.

### Multi-outcome arb extension (Prompt A — `arb_multi` table)

Built on top of strategy #2's detector. Reuses the same scanner, book
walk, completeness gate, and the events that pass `walk_band`
pre-filter. Differences vs the original bucket_arb path:

| aspect          | strategy #2 (`arb_positions`)              | extension (`arb_multi`)                 |
|-----------------|--------------------------------------------|-----------------------------------------|
| stake           | per-leg target share count (default 100)   | **fixed USD notional per set** ($10)    |
| threshold unit  | locked profit in USD (`min_arb_profit`)    | **net gap %** after fees (`min_net_gap_pct`, default 1%) |
| fees            | not modeled                                | **per-share fee** = `fee_taker_pct × vwap` |
| sub-threshold   | not logged (only full-walk detections)     | logged as `status='observed_below_threshold'` |
| depth shortfall | row not written                            | logged as `status='unfillable_leg'`     |
| exposure cap    | shared with single-leg paper book          | dedicated cap (`max_open_multi_sets`, default 20) |

The extension writes its rows to the new `arb_multi` table so the
daily report's MULTI-ARB section reads off a single source: every
walked event produces a row each cycle, and the distribution of
`net_gap_pct` across `observed_below_threshold` rows is the answer to
"how often do real gaps appear, even if too small to trade?" - the
research question motivating the build.

#### Polymarket fee schedule

Both `fee_maker_pct` and `fee_taker_pct` are config knobs. As of
2026-06 Polymarket's CLOB charges **zero protocol fees** on book
trades; the strategy ships with both at 0.0 to match. If the fee
schedule changes, set the new pct in config and the locked-profit
math stays net-of-fees automatically. The strategy applies
`fee_taker_pct × vwap` per share on a BUY (multi-arb always takes
liquidity to grab the full set in one shot).

#### Tests (4 new, on top of bucket_arb's 5)

- 3-outcome set summing to 0.97 → detected as arb (net_gap ≈ 3%)
- Thin book on one leg → `status='unfillable_leg'`, no position
- Sum 0.995 with 1% threshold → logged `observed_below_threshold`
- Fee math: 2% taker on each leg flips a marginally-positive gap
  negative (and the `fees` column is populated)
- Exposure cap of 1 blocks the second open

## Strategy #3: Cross-venue arbitrage (Polymarket x Kalshi)

The same real-world event can be listed on both Polymarket and Kalshi at
different prices. If you can buy YES-equivalent cheaply on one venue
AND NO-equivalent cheaply on the other such that total cost (after both
platforms' fees, slippage, and a safety buffer) is under $1.00, **one
side must pay $1** at resolution. That's the cross-venue arb.

The strategy's main risk isn't price - it's **the legs not being
equivalent**. Polymarket and Kalshi use different resolution sources
for the same nominal event (Polymarket weather = Wunderground / METAR;
Kalshi weather = NWS Climatological Report), different cutoff times,
sometimes different bucket widths. The rules-equivalence engine is
therefore the centerpiece of this build, not an afterthought.

### Rules-equivalence engine (`foundation/equivalence.py`)

For each candidate cross-venue market pair, the engine compares four
criteria:

| Criterion | What's checked | Failure modes seen in the wild |
|-----------|---------------|--------------------------------|
| Event identity (city + date + max/min) | Same real-world observation | Two different cities; different dates; high vs low temp |
| Cutoff time | `close_time` within 24h | Polymarket closes at the WU page's roll-over; Kalshi at NWS midnight local |
| Settlement condition | Identical bucket interval, canonicalized to Fahrenheit | Polymarket "14°C" bucket = [56.3F, 58.1F) vs Kalshi "57F" = [56.5F, 57.5F) — different widths |
| Resolution source | Same normalized source code (e.g. both `wunderground`) | Wunderground (Polymarket) vs NWS Climatological (Kalshi) — different instruments |

Each pair is classified:

- **CERTIFIED-IDENTICAL** — all four criteria match. Only these are
  ever auto-traded. The detector still book-walks every detected gap
  on every cycle.
- **FUZZY** — event identity matches but at least one of (cutoff,
  condition, source) differs. Persisted to `cv_pairs` for human review
  with a per-criterion verdict in `criteria_json`. **Never
  auto-traded.** This is the dominant outcome for weather pairs given
  the Wunderground/NWS source split.
- **NON-MATCH** — different real-world events; not logged.

### Divergence flagging (every certified gap carries the risk)

Even on a CERTIFIED-IDENTICAL pair, if the two venues' canonical source
codes match (e.g. both report `nws_climatological`) but the underlying
URLs differ (e.g. NWS Chicago Midway vs NWS Chicago O'Hare), the
engine attaches a `divergence_risk_note` to the pair. The note travels
all the way through to every `cv_gap` and every paper `cv_position`.

For weather pairs whose sources are *fundamentally different*
(Wunderground/METAR vs NWS Climatological), the pair classifies as
FUZZY and a more emphatic note is attached:

> Resolution sources differ: poly=Wunderground KORD vs kalshi=NWS
> Climatological Report Chicago. Both legs can lose simultaneously if
> the two sources disagree on the same day (Wunderground/METAR airport
> observation vs NWS Climatological Report can diverge by 1F on
> warm-front days).

Each `cv_position` table has a `status='DIVERGED'` outcome path: when
both legs lose at resolution, the grader marks it `DIVERGED` (the
risk materialized) and we book the full `-total_cost` as P&L. That's
the failure mode we're explicitly underwriting.

### Fees, slippage, and the locked-profit math

Per-share cost on each leg = `VWAP + slippage + fee_per_share`.

- **Polymarket fees**: 0 (paper). The current Polymarket protocol
  charges no maker/taker fees on the CLOB; the fee model is left as a
  config knob for when that changes.
- **Kalshi fees**: standard quadratic schedule, `fee_per_contract =
  ceil(fee_multiplier × 7c × price × (1 - price))`, rounded up to the
  next cent. Pulled from each series' metadata
  (`/series/{ticker}.fee_multiplier`).
- **Slippage**: 1c per leg by default, same as the rest of the
  foundation.
- **Safety buffer**: 0.5c per share on top.

Total locked profit per share = `1.00 - poly_cost - kalshi_cost - 0.5c`.
The paper executor fires only when **total** locked profit ≥
`min_arb_profit` ($0.50 default) AND the pair is `CERTIFIED-IDENTICAL`.

### Schema additions

Four new SQLite tables:

- `cv_pairs` — persistent classification of each cross-venue market
  pair: criteria verdicts, reason, divergence_risk_note, classification.
- `cv_gaps` — every detected gap, every cycle, with both legs'
  book-walked VWAP, fees, executable shares, locked profit, divergence
  risk note, and a `cleared_threshold` bit.
- `cv_positions` — paper-traded paired position (one row per fire),
  carrying the divergence_risk_note. Status: `OPEN | CLOSED | VOID |
  DIVERGED`.
- `cv_legs` — exactly two rows per `cv_position`, one for each venue's
  leg, with full fill details and eventual outcome + payout.

### Config knobs

```yaml
strategies:
  cross_venue_arb:
    safety_buffer: 0.005           # USD per share, on top of slippage + fees
    min_arb_profit: 0.50           # USD - paper-fire threshold
    target_shares: 50.0            # per-leg share target (capped by depth)
    poly_slippage_cents: 0.01      # per-share adverse slippage on Polymarket VWAP
    kalshi_slippage_cents: 0.01
    min_executable_shares: 5.0
    min_hours_to_resolve: 1.0
    execute_fuzzy: false           # NEVER auto-trade FUZZY pairs
    kalshi_categories:
      - Climate and Weather
```

### Live scan result (2026-06-10)

Cross-venue weather pass against Polymarket + Kalshi:

| metric                                                    | value |
|-----------------------------------------------------------|------:|
| Polymarket weather markets fetched                        | 1,266 |
| Kalshi daily-weather markets fetched                      |   482 |
| shared (city, date, kind) buckets (overlap universe)      |    14 |
| candidate pairs classified (after bucket-overlap prune)   | 18,359 |
| **CERTIFIED-IDENTICAL pairs**                             |   **0** |
| FUZZY pairs (logged for human review, never auto-traded)  | 18,359 |
| FUZZY pairs carrying source-divergence risk notes         | 18,359 |
| paper cv_positions fired                                  |     0 |

**Read of the result.** Every cross-venue weather pair classifies as
FUZZY because **Polymarket weather settles on Wunderground / METAR
station observations and Kalshi weather settles on the NWS
Climatological Report**. The two sources nominally measure the same
thing but use different instruments, different recording cadences, and
can disagree by 1°F on warm-front days. The equivalence engine
correctly refuses to certify any of these pairs as identical, and the
paper executor correctly fires zero trades.

That is the strategy's whole point as a safety rail: **a cross-venue
arb you can't certify is not free money — it's a coin flip with extra
steps.** The 18,359 FUZZY rows are persisted in `cv_pairs` with the
exact per-criterion diff (`criteria_json`) for a human reviewer to
override case-by-case. Expanding `kalshi_categories` to non-weather
markets (politics, sports, financials) is the natural next sweep,
since those sometimes share a published reference (e.g. CFPB rate
decisions cite the same FOMC press release on both venues).

### Manual run

```bash
python main.py cv             # one full scan: pair, classify, walk, log every gap, fire CERTIFIED
python main.py cv-stats       # diagnostics: pair counts by classification + gap distribution
```

The `cv` command runs only the cross-venue detector; `python main.py
cycle` runs all active strategies in one pass (weather + bucket_arb +
cross_venue_arb).

## Strategy #4: Copy-trading (paper)

PAPER ONLY. Public endpoints only - no wallet credentials, no order
placement, no signing libraries imported anywhere. Every endpoint used
is documented in `docs/api_notes.md` from a live probe; we never
fabricate fields.

### Three concerns, one module (`strategies/copy_trading.py`)

- **SCOUT** - roster build.
  - Discover candidates via the global `/trades` tape (paginate N pages
    and rank by USD activity) merged with `seed_wallets` from config.
    The public leaderboard endpoint promised in the build spec **does
    not exist** on data-api.polymarket.com - see `docs/api_notes.md`
    for the full list of 404s I probed. Auto-discovery via the trade
    tape is the substitute; a future leaderboard slots in cleanly via
    a third discovery source.
  - For each candidate, pull `/trades?user=<wallet>` incrementally
    using a per-wallet cursor (`wallet_cursors`), persist in
    `wallet_trades` (dedupe on `(transactionHash, asset, side)`).
  - Compute per-wallet metrics: track-record days, resolved-trade
    count, deployed USD, realized P&L on round-trip positions, ROI
    per trade, avg entry price, top single-close share of profit
    (lottery-winner detector), 6-month consistency, dominant category
    share, 30-day recent ROI.
  - **Hard filters** (any failure excludes the wallet, logged with a
    reason):
    - track record < 90d → excluded
    - resolved trades < 30 → excluded
    - avg entry price > 0.88 → excluded (the **favorite-grinder trap**)
    - days since last trade > 14 → excluded (nothing to copy)
    - top single-close share > 40% → excluded (lottery-winner profile)
    - lifetime ROI ≤ 0 or 30d ROI ≤ 0 → excluded
  - **Score** = 0.40 × lifetime ROI + 0.25 × consistency + 0.20 ×
    dominant-category share + 0.15 × 30d ROI. Weights are in config.
  - **Roster** (size 15 by default) with hysteresis: a wallet enters
    only after 2 consecutive scouts above rank 10; exits only after
    3 consecutive scouts below rank 25. Persistent in `roster` with
    per-wallet `hysteresis_state_json`.

- **FOLLOWER** - per-cycle paper copy.
  - For each ACTIVE roster wallet, pull trades since cursor, insert
    each new trade into `copied_trades`.
  - Simulate the $5 stake's fill against the **current** CLOB book
    (`scanner.fetch_book(token_id)`): BUY walks the asks ascending
    (+ 1c slippage); SELL walks the bids descending (- 1c slippage).
    The fill row records BOTH the leader's price AND ours, side by
    side, plus the top-3 book levels we walked through.
  - **Honesty rule**: if $5 cannot be filled within the visible book,
    we record `status='unfillable'` and do NOT invent a fill. The
    spec calls that valuable data, not an error.
  - Exposure caps from config: 40 open copies total, 8 per leader.
    At cap we record `status='skipped_cap'` so we know what we missed.

- **GRADER** - settle copied trades.
  - For each open copy whose market has resolved on Gamma, settle
    with our P&L (at our fill price) and the leader-equivalent P&L
    ($5 at THEIR price). The aggregate difference = **measured
    latency tax**. Per-leader aggregates surface in `copy-stats`.

### Backtester

`python main.py copy-backtest` replays each candidate wallet's last
90 days assuming a flat $5 stake at `leader_price + $0.02` penalty
(historical books are unavailable). Output is explicitly marked
**ESTIMATE** in every column. Ranks candidates; **does not predict
returns**.

### Schema additions

- `wallets` - per-wallet metrics snapshot.
- `wallet_trades` - persisted trade history, primary key on
  `(wallet, trade_key)` for cursor-based dedupe.
- `wallet_cursors` - last-pulled timestamp per wallet.
- `roster` - active leaders + hysteresis counters.
- `scout_snapshots` - one row per (date, wallet) with rank, score,
  passed/excluded + reason. Auditable.
- `copied_trades` - paper positions with both leader_price and
  our_price stored side by side, plus detection_delay_s,
  price_drift, status (`open | settled | unfillable | skipped_cap`),
  our_pnl, leader_pnl_equivalent.

### Config knobs

```yaml
strategies:
  copy_trading:
    stake_usd: 5.00
    slippage_cents: 0.01
    max_open_total: 40
    max_open_per_leader: 8
    discovery_pages: 5
    seed_wallets: []
    score_weights: {roi_lifetime: 0.40, consistency: 0.25, ...}
    hard_filters: {min_track_record_days: 90, ...}
    hysteresis: {roster_size: 15, ...}
```

### CLI

```bash
python main.py scout            # daily scout: discover, filter, score, roster
python main.py follow           # follower cycle: copy new trades from roster
python main.py copy-backtest    # replay last 90d (ESTIMATES only)
```

### What this first 30 days actually delivers

The deliverable is **the per-wallet latency-tax table**, not a P&L
forecast. Detection delay on this infrastructure is **minutes, not
seconds** - between cycles the leader's price has often moved. We
measure that bleed honestly so future infrastructure decisions
(faster cron, websockets, etc.) can be cost-justified or killed.

## Strategy #5: SHARPLINE (Prompt C, Phase 1)

PAPER ONLY. Maker-side value betting on Polymarket sports/esports vs
sharp bookmaker odds (Pinnacle and friends via The Odds API).

- **Vig removal**: convert two-way bookmaker decimal odds into a
  vig-free fair `P(YES)`.
- **Match**: fuzzy team-name token-set matcher; only auto-trade pairs
  with confidence ≥ 0.9. `AMBIGUOUS` matches (two Polymarket markets
  close on score) are logged but never auto-traded.
- **Edge**: post a resting paper limit at the current Polymarket YES
  ask when `(fair - ask) / ask ≥ edge_min` (default 7%).
- **Order lifecycle**: lifecycle columns reserved on `sharpline_orders`
  for `RESTING | FILLED | CANCELLED | UNFILLED_RESOLVED`, with
  `line_at_fill` and `adverse_selection`. v1 ships order **posting**;
  fill simulation lands in the next phase.
- **Honesty**: every row in `sharpline_orders` is tagged
  `estimate_marker='ESTIMATE'`. Maker fills cannot reflect real queue
  position, so all derived P&L is explicitly marked.
- **Adverse-selection sign convention (project-wide):**
  `POSITIVE = the line moved AGAINST our position before we got
  filled — we got picked off; the latency tax.` `NEGATIVE = the line
  moved IN FAVOR of our position; lucky fill.` Computed as
  `fair_prob_at_post − line_at_fill` on YES buys and the **negated**
  difference on NO buys so the sign means the same thing on either
  side. (Code: `strategies/sharpline.py:simulate_fills_and_grade`.)
- **Budget**: monthly request counter persists in `odds_api_log`;
  cap default 450, 30-min response cache in `odds_cache`. Caches
  never spend budget.
- **OBSERVE MODE**: if `ODDS_API_KEY` is missing, the strategy still
  runs - it scans Polymarket sports/esports markets and logs how many
  WOULD have been evaluated. Master report shows
  `sharpline: observe-only (no odds key)`.

## Strategy #6: LP-SIM (Prompt C, Phase 2)

PAPER ONLY. Simulates Polymarket liquidity-rewards quoting around the
midpoint, computes our would-be score per the published formula, and
estimates our share of the daily pool.

- **Reward score**: `((band - spread)^2 / band^2) × size × sided` where
  `sided = 1.0` two-sided, `0.5` one-sided. Tighter spreads quadratic-
  boost; one-sided depth gets half credit; size enters linearly to
  the reward-band cap. Implementation marker v1 in code.
- **Competition denominator**: unobservable from the public CLOB.
  Approximated as visible book depth in the reward band - documented
  on each row so the estimate's bias is explicit.
- **Output**: top `max_markets` ranked by score with estimated daily
  reward USD. Every row tagged `estimate_marker='ESTIMATE'`.

The simulator's job is to **confirm or kill** the hypothesis that
rewards + rebates + an in-house fair-value model can make LP profitable
on Polymarket, before any real capital is staked.

## Strategy #7: LOGIC-SCAN (Prompt C, Phase 3)

PAPER ONLY. Detects pairs of logically related markets and trades
violations of strict implication.

- **Templates**: champion vs finalist, winner vs advances, presidency
  vs nomination, "by date X" vs "by date Y". All require same
  `event_slug` on both markets (no cross-event matching).
- **Confidence floor**: only pairs at `confidence ≥ 0.95` are
  tradeable. Everything else (including lower-confidence detected
  pairs) is persisted to `logic_pairs` for human review. False
  implications are the failure mode, so transparency over volume.
- **Trade**: when `P(A) - P(B) ≥ min_margin` (default 3pp, net of
  fees), open the structural position; record to `logic_violations`
  with status `traded`. When `0 < margin < min_margin`, log
  `status='near_miss'` for distribution-tracking.
- **Stake**: $10 per leg, both-or-nothing; partial sets forbidden.

## V2 Operations runbook

PolyMarketBotV2 ships **paper-only**. There are no wallet credentials,
no signing libraries, and no order placement code anywhere in the
repo. Every API call is read-only against a public endpoint.

### CLI inventory

```bash
# 30-minute cadence (single-shot).
python main.py scan               # weather scanner only (no DB writes)
python main.py cycle              # weather + bucket_arb + cross_venue_arb in one pass
python main.py arb                # bucket-sum arb (Strategy #2) only
python main.py cv                 # cross-venue arb (Strategy #3) only
python main.py logic-scan         # logic-scan pair detection + violation check
python main.py lp-sim             # lp-sim quoting estimate pass

# 5-minute cadence.
python main.py follow             # copy-trading follower cycle
python main.py sharpline-post     # post sharpline resting orders (fetches odds)
python main.py sharpline-fill-cycle  # full RESTING -> FILLED/CANCELLED/UNFILLED_RESOLVED lifecycle

# Daily.
python main.py scout              # copy-trading roster build / hysteresis update
python main.py grade              # settles every strategy's resolved positions
python main.py copy-backtest      # copy-trading 90d backtest (ESTIMATEs)

# Reports / introspection.
python main.py report             # per-strategy detailed sections (V1 default)
python main.py master-report      # **V2 master report**: banner + scoreboard + sections
python main.py status             # quick per-strategy bankroll + open stake
python main.py bankroll           # bankroll snapshot + last 20 audit txns
python main.py arb-stats          # arb gap distribution
python main.py cv-stats           # cross-venue pair + gap distribution
```

### Scheduled workflows (`.github/workflows/`)

Workflow files are committed; they're scheduled by GitHub Actions
**when you push the repo**. Concurrency group `polymarketbot-state`
serializes their `git push` of `polymarketbot.db` back to main so
they never race.

| file        | cron           | runs |
|-------------|----------------|------|
| `cycle.yml` | `*/30 * * * *` | `cycle` + `logic-scan` + `lp-sim` + `status`. ~5-15 min. |
| `fast.yml`  | `*/5  * * * *` | `follow` + `sharpline-fill-cycle`. Under 1 min. Neither command burns Odds API budget. |
| `daily.yml` | `0 10 * * *`   | `scout` + **`sharpline-post`** (the only Odds-API-consuming step; sized at ~150 req/month) + `grade` + `master-report`. |

To activate: push to a private GitHub repo, then Settings → Actions
→ General → **Workflow permissions: Read and write** (so the
workflows can commit the DB back).

### Polymarket fee schedule (verified 2026-03)

Quadratic per-category taker fee `fee/share = rate × p × (1−p)`.
Rates live in `config.yaml:polymarket_fees.rates`, full source list
in `BUILD_NOTES.md`. Weather 1.25%, sports 0.75%, geopolitics 0%, etc.
Bucket-arb / cross-venue-arb / sharpline / logic-scan all subtract
the appropriate per-category fee when computing net P&L. A unit test
in `tests/test_fees.py` fails loudly if the rate table drifts from
the verified source.

### Where the data lives

- `polymarketbot.db` — single SQLite file. Schemas in
  `foundation/ledger.py`. Three families:
  - V1 weather: `markets`, `snapshots`, `signals`, `paper_trades`,
    `settlements`, `daily_report`.
  - Arb strategies: `arb_gaps`, `arb_positions`, `arb_legs`,
    `arb_multi`, `cv_pairs`, `cv_gaps`, `cv_positions`, `cv_legs`.
  - Copy-trading: `wallets`, `wallet_trades`, `wallet_cursors`,
    `roster`, `scout_snapshots`, `copied_trades`.
  - V2 ops layer: `bankroll_allocations`, `bankroll_transactions`,
    `equity_history`, `health_log`.

### How to read the master report

`python main.py master-report` produces a fixed-width, mobile-readable
report with three top-of-screen blocks:

1. **HEALTH banner** — one line. `HEALTH: OK` when every strategy's
   most recent run succeeded within its `stale_after_hours` threshold;
   otherwise a `|`-separated list of warnings ("weather: no
   successful run in 27h", "copy_trading: last run errored ...").
2. **Scoreboard** — one row per strategy with allocation %,
   starting alloc USD, current cash, open exposure, settled count,
   realized P&L, and a verdict column (`AHEAD | BEHIND | FLAT |
   TOO EARLY` per the configured `min_settled_for_pass` /
   `fail_loss_pct` thresholds).
3. **Per-strategy detailed sections** — same content as the V1 report.

The master report also **persists today's equity point** for every
strategy to `equity_history` so multi-day curves are queryable.

### Bankroll mechanics

- One total bankroll (`$1,000` default), split by percent allocation
  across strategies in `config.yaml`'s `bankroll.allocations`. The
  percentages are auto-renormalized to 100%, so weights can be
  changed without re-balancing them by hand.
- Every paper position goes through `Bankroll.try_debit(strategy,
  stake)` before being recorded. If the strategy is out of cash the
  position is skipped with a `kind='skipped_no_capital'` row in
  `bankroll_transactions` — visible in `python main.py bankroll`.
- At settlement the grader calls `Bankroll.credit(strategy, proceeds,
  opening_stake)`. Net P&L = `proceeds - stake`.
- The audit trail is complete: every `bankroll_allocations` value
  for any strategy can be reconstructed from the
  `bankroll_transactions` log.

### Health & resilience

Every strategy's run is wrapped in a `HealthSession` context manager
(`foundation/health.py`). An unhandled exception inside one strategy
is recorded to `health_log` and swallowed; **other strategies still
run their cycle.** The banner surfaces failures on the next
`master-report`.

Configurable per-strategy stale thresholds live under `health:` in
`config.yaml`.

### Reminder: PAPER ONLY

There are no credentials anywhere in this repo. `python main.py *`
never sends an order, never signs a transaction, never reads a
private key. The only network calls are GETs to:

- `gamma-api.polymarket.com` — markets + events
- `clob.polymarket.com` — order books
- `data-api.polymarket.com` — wallet trade history (copy-trading)
- `api.elections.kalshi.com` — markets + orderbook (cross-venue)
- `open-meteo.com` — weather forecast (Strategy #1)
- `api.weather.com` — Wunderground history (Strategy #1 grader)

## Honest expectations

Read sec 11 of the build plan. Week 1 is a systems test, not a verdict.
Brier < 0.18 over 30+ trades is what proves the strategy has skill; a
green week proves nothing. The pluggable foundation is the actual asset —
it makes the next strategy a 1-file experiment.
