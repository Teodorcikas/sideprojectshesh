# CS2 Trade-Up EV Calculator

Tools for finding profitable CS2 trade-up contracts by scanning market prices and calculating expected value.

## Files

| File | Purpose |
|------|---------|
| `ev_calculator.py` | Main tool. 3-phase scanner that finds profitable trade-ups |
| `dmarket_prices.py` | Standalone DMarket price fetcher for low-float FT skins |
| `pricempire_prices.py` | Pricempire API wrapper (subscription expired, not used) |
| `winners.md` | Persistent log of all profitable trade-ups found across runs |
| `price_cache.json` | Auto-generated cache for CSFloat output prices (3h TTL) |
| `skinport_cache.json` | Auto-generated cache for Skinport prices (3h fresh, 6h stale fallback) |
| `dmarket_cache.json` | Auto-generated cache for DMarket listings (3h fresh, 6h stale fallback) |
| `csfloat_input_cache.json` | Auto-generated cache for CSFloat input listings (6h fresh) |
| `skinport_lastcall.txt` | Rate limiter tracker for Skinport API (60s cooldown) |
| `opportunities_cache.json` | Saved profitable opportunities with listing IDs for verification |

## APIs Used

| API | Purpose | Auth | Status |
|-----|---------|------|--------|
| **DMarket** | Input listings with float values | None required | Working |
| **Skinport** | Input prices (often cheaper than DMarket) + output fallback | None, but rate limited | Working (60s cooldown) |
| **CSFloat** | Output prices (where we sell) | API key in file | Working (4 req/s global rate limit) |
| **Steam Market** | Volume/trend data for outputs | None | Working |
| **CSGO-API** (GitHub) | Skin database with float ranges | None | Working |
| **Pricempire** | Alternative pricing | API key expired | Not used |

## Trade-Up Strategy

**Core approach:** Buy 10 low-float skins, trade up to guaranteed higher-condition outputs.

1. Find skins with floats low enough to guarantee MW (or FN) outputs
2. Calculate max input float per skin: `max_input = (0.15 - out_min) / (out_max - out_min)`
3. Output float formula: `output_float = avg_adjusted_input * (out_max - out_min) + out_min`
4. Adjusted float per skin: `(raw - skin_min) / (skin_max - skin_min)`
5. Profit when: `expected_output_value > input_cost + fees`

**Fees:**
- CSFloat seller fee: 2%
- Steam seller fee: 15% (for comparison only)
- No buyer fees on DMarket/Skinport

## Output Price Sourcing (important logic)

Output prices are fetched from CSFloat (where we sell). Rules:

1. **CSFloat has listing** → use CSFloat price directly
2. **CSFloat confirms no listing** (200 OK, empty results) → marked `NO_CSFLOAT_LISTINGS`, excluded from EV. No Skinport fallback. A skin with no CSFloat listings cannot be sold there.
3. **CSFloat rate-limited/error** (429 or network fail) → Skinport fallback, but only if Skinport has **≥ 2 listings** (singleton listings are unreliable price signals — e.g. one seller listing at 10× market price)

**Why quantity ≥ 2 for Skinport fallback:** A single Skinport listing can be priced at anything (e.g. SSG 08 Orange Filigree MW had 1 listing at $1,365 vs real market price of ~$53). Multiple listings indicate a real market.

## CSFloat Rate Limiting

CSFloat enforces a request rate limit. The code uses:

- **Global `RateLimiter` class** (token bucket): enforces max **4 req/s** (250ms between requests) across all threads
- **Exponential backoff on 429**: retries at 5s → 10s → 20s → 40s delays
- **No caching of failed results**: if all retries fail (returns 0), the result is NOT written to cache — next run will retry fresh
- **`CSFLOAT_NO_LISTING = -1` sentinel**: distinguishes "confirmed no listing" from "rate-limited unknown"

## Current State (v3.3)

**Working:**
- Fetches from DMarket (47,000+ items) + CSFloat (50 items via price-based pagination) + Skinport
- **Skin-targeted DMarket fetch**: fetches by exact skin name per collection+rarity instead of weapon type, so rare/expensive skins are not crowded out
- **Deduplication by listing_id** to prevent counting same listing multiple times
- Dynamic float limits per collection based on output skin float ranges
- Groups by collection, requires 10+ inputs of same rarity (liquidity check)
- Separates StatTrak/non-StatTrak (can't mix in trade-ups)
- **Parallel Phase 2 output price pre-fetch**: 5 workers, 4 req/s global rate limit
- CSFloat output prices with 2% fee calculation; Skinport fallback (qty ≥ 2) for rate-limited results
- Steam volume/trend data for profitable trade-ups only
- ROI filter: only shows 25%+ ROI AND $0.30+ EV trade-ups — **do not lower this threshold**
- WATCH LIST: Shows collections with 5-9 inputs (close to executable)
- **Opportunity tracking:** Saves profitable trade-ups with exact listing IDs to `opportunities_cache.json`
- **Verification on startup:** Checks if saved listings still exist and prices remain profitable
- **Winners log:** Appends all profitable results to `winners.md` with date, ROI, EV, buy links
- Caching: 6h for inputs, 3h for output prices

## TODO

### High Priority
- [x] **Add CSFloat as input source** — Done
- [x] **Skin-targeted DMarket fetch** — Done (fetches by exact skin name, not weapon type)
- [x] **Parallel output price fetching** — Done (5 workers, global rate limiter)
- [x] **CSFloat rate limit handling** — Done (token bucket + exponential backoff)
- [x] **Skinport output fallback** — Done (qty ≥ 2 guard, NO_CSFLOAT_LISTINGS exclusion)
- [ ] **Rarity expansion** — Support all rarity tiers more broadly
- [ ] **Quick execution solution** — Auto-buy inputs or one-click purchase flow
- [ ] **Steam 14-day price trend** — Add price history as trend indicator

### Medium Priority
- [ ] **More marketplace integrations** — Buff163, Waxpeer, CS.Money
- [ ] **Add buy order support** — Place buy orders at target prices
- [ ] **Auto-refresh mode** — Run every X minutes and alert on new opportunities

### Low Priority
- [ ] **Integrate Pricempire** — Needs new subscription
- [ ] **Filter by Steam volume** — Skip illiquid outputs (<5 sales/day)
- [ ] **StatTrak trade-up support** — Show ST opportunities separately

### Ongoing
- [ ] **Bug fixes** — Monitor and fix issues as they arise

## Key Constants

```python
SKINPORT_COOLDOWN = 60            # 1 minute between Skinport API calls
CACHE_EXPIRY = 3 * 60 * 60        # 3 hours fresh (output prices)
INPUT_CACHE_EXPIRY = 6 * 60 * 60  # 6 hours fresh (input listings)
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours stale fallback
MIN_ROI = 25.0                    # Only show 25%+ ROI — do not lower
MIN_EV = 30                       # Only show $0.30+ net profit (in cents)
CSFLOAT_SELLER_FEE = 0.02         # 2% when selling on CSFloat
CSFLOAT_NO_LISTING = -1           # Sentinel: confirmed no CSFloat listing
# Global rate limiter: 4 req/s for all CSFloat requests
```

## Quick Start

```bash
cd "C:\Users\Namai\cs2-bot"
python ev_calculator.py
```

First run fetches fresh data (~2 min for output prices at 4 req/s). Subsequent runs within 3h use cache (~10s).
