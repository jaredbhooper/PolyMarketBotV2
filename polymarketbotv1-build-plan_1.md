# PolyMarketBotV1 — Paper-Trading Foundation + Pluggable Strategies
**Codename:** PolyMarketBotV1 (formerly Drizzle)
**Goal:** A reusable, fully automated paper-trading rig for Polymarket. The **foundation** (scanner, paper executor, ledger, grader, reporter, scheduler) is strategy-agnostic and permanent. **Strategies** are swappable plug-in modules that take market data in and emit probability estimates. Strategy #1 is the weather/temperature model. If weather shows no edge, we swap the module — the foundation keeps running. Built in one day with Claude Code. Zero spend in paper mode — every API is free, no accounts, keys, or wallets required.

---

## 0. Design principle: foundation vs strategy

```
FOUNDATION (permanent, strategy-agnostic)
├── Market scanner          — reads ANY Polymarket market
├── Edge engine + paper executor — generic: model P vs ask price
├── Ledger (SQLite)         — logs signals & trades from any strategy
├── Grader                  — settles trades, scores calibration
├── Reporter                — daily summary
└── Scheduler               — GitHub Actions / cron

STRATEGY MODULES (swappable, implement one interface)
└── strategies/weather.py   — Strategy #1: ensemble temperature model
    strategies/<next>.py    — future: arb, news-reaction, etc.
```

### The strategy interface (the contract every module obeys)

```python
class Strategy(ABC):
    name: str                      # e.g. "weather_v1"

    @abstractmethod
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        """Filter the scanner's full market list down to the ones
        this strategy knows how to price."""

    @abstractmethod
    def estimate(self, market: Market) -> Estimate | None:
        """Return a probability estimate for the market, or None to skip.
        Estimate = {p_final: float, confidence: float, metadata: dict}"""
```

Rules of the contract:
- A strategy **never** touches the ledger, the executor, or prices directly. It only prices probability. The foundation decides whether that probability is an edge worth (paper) trading.
- All strategy-specific data (ensemble members, model disagreement, etc.) goes in `metadata` — the ledger stores it as JSON so the grader can analyze any strategy without schema changes.
- `confidence` lets a strategy ask the foundation to size down (e.g. weather sets confidence 0.5 when GFS and ECMWF disagree).
- Active strategies are listed in `config.yaml`; adding a strategy = adding a file + one config line. No foundation code changes.

---

## 1. Success criteria (define before building)

After 7 days, the system is "working" if:
1. **Calibration:** When the active strategy said 80%, did the outcome happen ~80% of the time? Measured via Brier score (target < 0.18; a coin-flipper scores 0.25).
2. **Paper P&L after costs:** Positive after simulated spread + 1c slippage per fill. Track this, but ONE week is too small a sample to prove an edge — 7 days might be 15–30 trades. Treat week 1 as a systems test, weeks 2–4 as the real evaluation.
3. **Uptime:** Bot ran every cycle without manual intervention and graded every resolved market.

**Not a success criterion:** big paper profits in week 1. A hot week proves nothing; a calibrated month proves something.

**Foundation-level success (matters even if weather fails):** the rig runs unattended, fills honestly, grades correctly, and can accept a new strategy module without rework. That's the asset.

---

## 2. Architecture overview

```
┌─────────────────────────────────────────────────────┐
│  SCHEDULER (GitHub Actions cron OR local cron/pm2)  │
│  runs main.py every 30 min                          │
└────────────────────┬────────────────────────────────┘
                     ▼
   ┌─────────────────────────────────────┐
   │ 1. MARKET SCANNER        [foundation]│
   │ Polymarket Gamma API → all open      │
   │ markets, prices, books               │
   └────────────────┬────────────────────┘
                    ▼
   ┌─────────────────────────────────────┐
   │ 2. STRATEGY LAYER         [plug-in] │
   │ for each active strategy:           │
   │   relevant_markets() → estimate()   │
   │ Weather v1: Open-Meteo Ensemble API │
   │ GFS (31) + ECMWF (51) members per   │
   │ station → P(threshold hit)          │
   └────────────────┬────────────────────┘
                    ▼
   ┌─────────────────────────────────────┐
   │ 3. EDGE ENGINE + PAPER EXECUTOR     │
   │ [foundation] model P vs market ask →│
   │ if edge > threshold, simulated fill │
   └────────────────┬────────────────────┘
                    ▼
   ┌─────────────────────────────────────┐
   │ 4. LEDGER (SQLite) + GRADER         │
   │ [foundation] log signals/trades;    │
   │ next morning fetch actuals, settle, │
   │ score per-strategy                  │
   └────────────────┬────────────────────┘
                    ▼
   ┌─────────────────────────────────────┐
   │ 5. REPORTER  [foundation]           │
   │ daily summary → console/Telegram/SMS│
   └─────────────────────────────────────┘
```

Single Python repo, SQLite, no frontend needed (add later if wanted).

```
PolyMarketBotV1/
├── main.py            # cycle + grade entrypoints
├── config.yaml        # active strategies, cities, thresholds
├── foundation/
│   ├── scanner.py
│   ├── edge.py
│   ├── executor.py
│   ├── ledger.py
│   ├── grader.py
│   └── report.py
└── strategies/
    ├── base.py        # Strategy ABC + Estimate dataclass
    └── weather.py     # forecast fetch + probability model
```

---

## 3. Platforms & APIs — every connection you need

### 3.1 Polymarket (market data — the venue we paper-trade)
- **Weather markets page (eyeball it):** https://polymarket.com/weather — daily "Highest temperature in <city> on <date>" markets for global cities (London, Paris, NYC, etc.)
- **Gamma API (market discovery, REST, no auth for reads):** `https://gamma-api.polymarket.com/markets` and `/events` — the scanner pulls broadly; each strategy filters via `relevant_markets()` (weather filters tag/slug containing "highest-temperature").
- **CLOB API (prices & order books, no auth for reads):** `https://clob.polymarket.com` — endpoints: `/book?token_id=...` (full order book), `/price?token_id=...&side=buy`, `/midpoint?token_id=...`
- **Python client:** `pip install py-clob-client` (only needed if we ever go live; raw `requests` is fine for paper)
- **Docs:** https://docs.polymarket.com

### 3.2 Open-Meteo (the weather strategy's signal source)
- **Ensemble API (core of weather v1):** `https://ensemble-api.open-meteo.com/v1/ensemble`
  - Example call:
    `https://ensemble-api.open-meteo.com/v1/ensemble?latitude=51.4775&longitude=-0.4614&hourly=temperature_2m&models=gfs_seamless,ecmwf_ifs025&forecast_days=3&timezone=auto`
  - Free, **no API key**, JSON. Returns one time series per ensemble member (`temperature_2m_member01`, `...member02`, etc.). GFS ≈ 31 members; ECMWF ≈ 51 members.
  - Rate limit: 10,000 calls/day free — we need ~50/day. Plenty.
- **Docs:** https://open-meteo.com/en/docs/ensemble-api
- **Historical/actuals (for grading):** `https://archive-api.open-meteo.com/v1/archive` or the forecast API's `past_days` parameter for observed station-area temps.

### 3.3 Resolution sources (what decides "truth" — read carefully, this is where edges and traps live)
- **Polymarket temperature markets resolve via the Weather Underground "History" tab for the designated airport station** (e.g. wunderground.com history for LHR/Heathrow, KNYC, etc.). NOT the NWS report.
- **Kalshi (if we ever extend) resolves via the next-day NWS Climate Report (CLI)** — a different source. The same day at the same station can settle differently across platforms.
- **Trap:** NWS CLI uses *local standard time* — during daylight saving, the "day" runs 1:00 AM to 12:59 AM. Weather Underground resets at midnight clock time. Our grader must replicate the exact resolution source per market: read each market's rules text from the Gamma API and store the resolution source + station with the trade.
- **Grading data:** scrape/fetch the station's observed daily max. Primary: Open-Meteo archive at the station's exact coordinates. Verification: the Weather Underground history page for that station (HTML fetch, parse the daily max).
- **Foundation note:** the grader's "fetch truth" step is strategy-pluggable too — each strategy registers a `resolve(market) -> outcome` helper, since different market classes resolve from different sources.

### 3.4 Optional alerting
- Telegram Bot API (free): daily report + "new paper trade" pings. Or wire it into the existing Telnyx SMS module from the HDS stack.

### 3.5 Reference implementations (read, don't blindly run)
- https://github.com/suislanchez/polymarket-kalshi-weather-bot — same concept: 31-member GFS via Open-Meteo, edge > 8% trigger, 15% fractional Kelly, Brier tracking. Use as architecture reference.
- https://github.com/Polymarket/agents — official Polymarket AI-agent framework (MIT).
- Kalshi KXHIGHNY miscalibration analysis (evidence the edge class is real): zerve.ai gallery, "CalibShi" notebook — 8,494 settled NYC temp markets analyzed for systematic mispricing.
- **Security rule:** never paste a wallet private key into anything during this project. Paper mode needs no keys at all. Any repo demanding a private key for "research" gets read, not run.

---

## 4. Market universe (weather strategy v1)

Start narrow: **5 cities** with active daily Polymarket temp markets and good ensemble model coverage:
- London (Heathrow), Paris (Orly/CDG — check market rules for which), New York (KNYC/LaGuardia — check rules), Miami, Chicago
- Store per city: `{market_slug, station_name, station_lat, station_lon, resolution_source, units (°C/°F), timezone}` — this lives in `config.yaml` under the weather strategy's section.
- **Forecast the station's exact coordinates, not the city center.** Heathrow ≠ central London by 1-2°C on some days. This detail alone separates winners from vibes traders.
- Only trade markets resolving **today or tomorrow** (ensemble skill degrades fast beyond 48h; near-dated markets also have the most mispricing as casuals anchor on stale forecasts).

---

## 5. Probability model (weather strategy internals)

### v1 — raw ensemble fraction (build first, 30 minutes)
For market "High temp in London ≥ 25°C on June 12":
1. Pull all ensemble members' hourly `temperature_2m` for the station coords.
2. For each member, take the max over the market's resolution window (mind the timezone + DST quirk).
3. `P = members_above_threshold / total_members` (pool GFS + ECMWF = up to ~82 members).

### v2 — smoothing (build today, +1 hour)
Raw fractions are jumpy near thresholds (28/31 = 90.3%, but is it really?). Fit a normal distribution to the member maxima (mean μ, std σ) and compute `P = 1 - CDF(threshold)`. Optionally blend: `P_final = 0.5 * raw_fraction + 0.5 * gaussian_P`.

### v3 — bias correction (week 2+, after data accumulates)
Models run systematically hot/cold per station (urban heat island, sensor siting). After ~2 weeks of (forecast, actual) pairs, fit a per-station offset and apply before computing P. This is where the edge compounds. Open-Meteo's Previous Runs / Historical Forecast APIs let you backfill (forecast, observation) pairs immediately instead of waiting — worth doing in week 1.

### Sanity rails
- If GFS and ECMWF disagree wildly (|P_gfs − P_ecmwf| > 0.25), the strategy returns `confidence = 0.5` (foundation halves the stake) or `None` (skip) — model uncertainty is itself information.
- Never output P = 0 or 1; clamp to [0.02, 0.98].

---

## 6. Edge engine & paper execution rules (foundation — applies to every strategy)

```
edge = P_model − ask_price          (for YES; symmetric for NO using its ask)
TRADE if edge ≥ 0.08                (8% default; per-strategy override in config)
SKIP if book depth at ask < $50     (the market can't actually absorb even a small real order — paper fills must be honest)
SKIP if market resolves < 2h away   (avoid stale-signal races near close)
SKIP if spread > 6c                 (illiquid junk)
```

**Paper fill rules (be pessimistic, or the backtest lies to you):**
- **Book-walking fills:** execute against the real order book snapshot — consume liquidity level-by-level (if the stake exhausts the best ask, the remainder fills at the next price level up), then add 1c adverse slippage on the volume-weighted average price. Store levels consumed in the trade record. Never fill at the midpoint.
- Paper bankroll: $1,000 (decimal-tracked, fake). One shared bankroll, P&L tracked **per strategy** so each module gets its own verdict.
- **Sizing: fractional Kelly at 15%** — `stake = bankroll × 0.15 × kelly_fraction(P_model, price) × confidence`, capped at $50/market and max 6 open positions.
- One position per market per day. Hold to resolution (v1 has no early exits — simpler to grade).

---

## 7. Data model (SQLite: `polymarketbot.db`)

```sql
markets(id, slug, category, threshold, unit, resolve_date, resolution_source, rules_text, created_at)
snapshots(id, market_id, ts, yes_ask, yes_bid, no_ask, no_bid, book_depth_usd)
signals(id, market_id, strategy, ts, p_final, confidence, metadata_json)
paper_trades(id, market_id, strategy, ts, side, price_filled, stake, p_model_at_entry, edge_at_entry, status)
settlements(id, market_id, actual_value, source_value, outcome, settled_at)
daily_report(date, strategy, n_trades, n_wins, pnl, brier, bankroll)
```

Changes vs Drizzle: `forecasts` is generalized to `signals` with a `strategy` column and JSON metadata (weather stores member maxima, p_raw, p_gauss there); `paper_trades` and `daily_report` carry `strategy` so calibration and P&L are scored per module. Every cycle logs a snapshot + signal **even when not trading** — that no-trade data powers bias correction and proves/disproves the edge later.

---

## 8. Scheduler & deployment (free options)

**Option A — GitHub Actions (recommended, zero infra):** cron `*/30 * * * *` workflow runs `python main.py cycle`, commits `polymarketbot.db` back to a private repo (or uploads as artifact). Free tier minutes are more than enough. Survives your laptop being off. Note: schedule triggers can drift 5–15 min on the free tier — irrelevant at our timescale.
**Option B — local machine:** `cron`/Task Scheduler or `pm2` if the machine stays on.
**Option C — $5/mo VPS:** only if A and B annoy you. Not needed for paper.

A second job (`python main.py grade`) runs daily at ~10:00 local per station's morning to fetch actuals, settle yesterday's trades, update Brier + P&L per strategy, and send the report.

---

## 9. Build order for today (Claude Code session plan)

Each step is one Claude Code prompt. Test after each before moving on. Foundation first, strategy second — that's the whole point.

1. **Skeleton (20 min):** repo `PolyMarketBotV1/` with the `foundation/` + `strategies/` layout above, `strategies/base.py` defining the Strategy ABC and Estimate dataclass, SQLite init, `config.yaml` with active strategies + the 5 cities.
2. **Market scanner (60 min) [foundation]:** Gamma API query for open markets → extract slug, threshold, date, token IDs, rules text → CLOB book fetch for asks/bids/depth. Print a table. (Expect to iterate — tag/slug filtering is the fiddliest part.)
3. **Edge engine + paper executor + ledger (75 min) [foundation]:** generic pipeline that takes (market, Estimate) pairs → applies all skip-rules → **book-walking simulated fills per section 6** (walk the CLOB order book snapshot level-by-level, VWAP + 1c) → SQLite writes. Test it with a dummy strategy that returns hardcoded probabilities, and verify a fill against a hand-calculated walk of one real order book.
4. **Weather strategy: forecast engine (45 min) [strategies/weather.py]:** Open-Meteo ensemble fetch for one station → parse members → daily-max per member over a given local-time window → unit conversion °C/°F. Unit-test against today's London forecast by hand.
5. **Weather strategy: probability model (45 min):** raw fraction + gaussian smoothing → implement `relevant_markets()` + `estimate()` against the ABC → swap out the dummy strategy in config.
6. **Grader (45 min) [foundation]:** fetch actuals (Open-Meteo archive; WU page parse as cross-check) → settle → Brier + P&L per strategy → daily_report rows.
7. **Reporter + scheduler (30 min) [foundation]:** console/Telegram summary; GitHub Actions workflow file; dry-run one full cycle end-to-end.

Total: ~4-5 focused hours.

**Kickoff prompt to paste into Claude Code:**
> Read polymarketbotv1-build-plan.md in this folder. Build the entire system following the 7 build steps in section 9, in order — foundation modules first, then the weather strategy as a plug-in implementing the Strategy ABC in section 0. Test each module with real API calls as you complete it before moving to the next (use the dummy strategy to test the foundation pipeline before weather exists). Use SQLite per section 7, config.yaml for active strategies and the 5 cities, and follow the paper-fill rules in section 6 exactly. When everything is built, run one full end-to-end cycle and show me the scanner table, the weather strategy's probabilities, and any paper trades it would have made. Create a README covering: how to run a cycle manually, how to set up the GitHub Actions scheduler from section 8, and how to add a new strategy module.

---

## 10. Week-1 evaluation protocol

Daily, automatically: report shows open positions, settled trades, running Brier, running paper P&L vs the $1,000 start — per strategy.
End of week, manually review:
- **Brier < 0.18 and trades ≥ 15:** promising — keep running, start v3 bias correction.
- **Brier ~0.25:** model has no skill yet — debug station coords, timezone windows, resolution-source mismatches before touching strategy logic.
- **Good Brier but negative P&L:** edge threshold too low for the spread — raise to 10-12% for this strategy in config.
- **Any settlement that surprised you:** read that market's resolution rules again. 90% of "bad luck" in weather markets is a resolution-rule misread.
- **If weather fails after weeks 2-4:** retire the module, keep the foundation. Next candidate strategies to spec: cross-platform price comparison (Polymarket vs Kalshi), news-reaction lag on resolution-imminent markets. Each gets the same paper gauntlet before any other conversation happens.

---

## 11. Honest expectations (read once, believe it)

- Paper P&L at $1,000 fake bankroll, even if everything works, projects to tens of dollars/week real — this is an edge *detector*, not an income stream yet.
- The known players (wallets grinding weather full-time) are running bias-corrected multi-model blends. v1-v2 of this bot is below their level; v3 with a month of station-specific calibration data starts to compete in the less-watched city markets.
- The pluggable foundation makes *experiments* cheap. It does not make any experiment likely to win — every new strategy faces the same efficient-market problem weather does. The rig's value is fast, honest verdicts.
- The decision to ever put real money in happens after ≥30 days of positive, calibrated, honestly-slippaged paper results — and gets its own conversation about platform access, withdrawal fees, and bankroll you can afford to lose.
- The real week-1 win: a running autonomous system, a clean dataset, and a verdict either way. That's worth more than a lucky green week.
