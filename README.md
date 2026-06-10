# PolyMarketBotV1

Paper-trading rig for Polymarket. A strategy-agnostic foundation (scanner →
edge engine → paper executor → SQLite ledger → grader → reporter) wraps a
pluggable strategy interface. Strategy #1 is a weather/temperature ensemble
model (Open-Meteo GFS + ECMWF → P(threshold hit)).

Built per `polymarketbotv1-build-plan_1.md`. Zero spend in paper mode — every
API used (Polymarket Gamma, Polymarket CLOB, Open-Meteo) is free and
authenticationless.

## Repo layout

```
PolyMarketBotV1/
├── main.py                # entrypoints: cycle, scan, grade, report, status
├── config.yaml            # active strategies + 5 cities + paper params
├── foundation/            # strategy-agnostic, permanent
│   ├── scanner.py         #   Gamma API + CLOB book → Market objects
│   ├── edge.py            #   YES/NO edge helpers
│   ├── executor.py        #   skip-rules + book-walking paper fills
│   ├── ledger.py          #   SQLite (markets, snapshots, signals, paper_trades, settlements, daily_report)
│   ├── grader.py          #   settles markets; computes Brier + P&L per strategy
│   └── report.py          #   console summary; optional Telegram hook
└── strategies/            # swappable plug-ins
    ├── base.py            #   Strategy ABC + Estimate dataclass
    ├── dummy.py           #   bench strategy for testing the foundation
    └── weather.py         #   Strategy #1: ensemble temperature model
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

## Honest expectations

Read sec 11 of the build plan. Week 1 is a systems test, not a verdict.
Brier < 0.18 over 30+ trades is what proves the strategy has skill; a
green week proves nothing. The pluggable foundation is the actual asset —
it makes the next strategy a 1-file experiment.
