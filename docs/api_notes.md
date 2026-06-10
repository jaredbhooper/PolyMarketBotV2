# Polymarket public API notes (probed 2026-06-11)

This file documents the actual response shapes returned by the public
Polymarket data API endpoints we hit from the copy-trading strategy.
Never use field names that are not listed here; if a new endpoint is
needed, probe it first, document, then write.

## https://data-api.polymarket.com

### GET /trades

Public global trade tape. Supports `?user=<proxyWallet>` to filter to
one wallet's history. Paginated via `limit` (default 100, max ~500).

Observed item shape (2026-06-11):

```
{
  "proxyWallet":  "0x...",                       # wallet that traded
  "side":         "BUY" | "SELL",
  "asset":        "<token id, 256-bit decimal>",
  "conditionId":  "0x<32 bytes>",
  "size":         <float, shares>,
  "price":        <float, USD per share>,
  "timestamp":    <unix seconds>,
  "transactionHash": "0x<64 hex>",
  "title":        "Will Israel close its airspace by June 15?",
  "slug":         "...",
  "eventSlug":    "israel-closes-its-airspace-by",
  "outcome":      "Yes" | "No",
  "outcomeIndex": 0 | 1,
  # display-only
  "name", "pseudonym", "bio", "icon", "profileImage", "profileImageOptimized"
}
```

NB: there is **no** numeric trade id - dedupe by
`(transactionHash, asset, side)`.

### GET /trades?user=<proxyWallet>

Same shape. Add `&offset=` for pagination cursor.

### GET /positions?user=<proxyWallet>

```
{
  "proxyWallet":      "0x...",
  "asset":            "<token id>",
  "conditionId":      "0x...",
  "size":             <float, shares held>,
  "avgPrice":         <float>,
  "initialValue":     <float, USD>,
  "currentValue":     <float, USD>,
  "cashPnl":          <float>,
  "percentPnl":       <float>,
  "totalBought":      <float, lifetime USD on this token>,
  "realizedPnl":      <float>,
  "percentRealizedPnl": <float>,
  "outcome":          "Yes" | "No",
  "outcomeIndex":     0 | 1,
  "oppositeAsset":    "<token id of NO when this is YES>",
  "oppositeOutcome":  "Yes" | "No",
  "endDate":          "<ISO>",
  "eventId":          "<gamma event id>",
  "eventSlug":        "...",
  "icon", "slug", "title",
  "mergeable":        bool,
  "negativeRisk":     bool,
  "redeemable":       bool
}
```

### GET /activity?user=<proxyWallet>

Same as /trades plus REDEEM / MERGE / SPLIT / etc. via the `type`
field. Item shape adds:

```
{
  "type":     "TRADE" | "REDEEM" | "MERGE" | "SPLIT" | "REWARD" | ...,
  "usdcSize": <float>     # USD notional of this activity
}
```

### Leaderboard endpoints

**None reachable** as of 2026-06-11. Tried:
`/leaderboard`, `/leaderboards`, `/leaderboards/profit`,
`/profit-leaderboard`, `/volume-leaderboard`, `/leaders`,
`/top-traders`, `/profit`, `/volume`, `/rankings`, `/users-pnl`,
`/top`, `/leaderboard?window=1m&rankType=profit` - all 404.

The scout therefore discovers candidate wallets either:
  (a) from `copy_trading.seed_wallets` in config (operator-supplied), and/or
  (b) by aggregating the public `/trades` tape over the discovery window:
      pull N pages of recent global trades, group by `proxyWallet`,
      rank by activity (trade count + USD notional) within window.

Both paths flow through the same filter + scoring pipeline so a future
leaderboard endpoint can be slotted in via a third discovery source
without changing scoring.

## https://gamma-api.polymarket.com

### GET /markets?condition_ids=<conditionId>

Returns the market with closed/outcomePrices fields once resolved.
Used by the grader to settle copied trades.

## https://clob.polymarket.com

### GET /book?token_id=<asset>

Order book snapshot for a single token. Used at copy time to simulate
a paper fill at our price (walk asks for BUY / walk bids for SELL +
1c slippage). Top-3 levels are stored as `book_snapshot_json` on each
copied trade for later diagnosis.
