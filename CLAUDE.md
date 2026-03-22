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
| `waxpeer_cache.json` | Auto-generated cache for Waxpeer listings (6h fresh) |
| `skinport_lastcall.txt` | Rate limiter tracker for Skinport API (60s cooldown) |
| `opportunities_cache.json` | Saved profitable opportunities with listing IDs for verification |

## APIs Used

| API | Purpose | Auth | Status |
|-----|---------|------|--------|
| **DMarket** | Input listings with float values | None required | Working |
| **Skinport** | Input prices (often cheaper than DMarket) + output fallback | None, but rate limited | Working (60s cooldown) |
| **CSFloat** | Output prices (where we sell) | API key in file | Working (4 req/s global rate limit) |
| **Waxpeer** | Input listings with float values | API key (free) | Working |
| **Steam Market** | Volume/trend data + output price verification | None | Working |
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

## Current State (v3.5)

**Working:**
- Fetches from DMarket (47,000+ items) + CSFloat (1,700+ via targeted rarity fetch) + Waxpeer + Skinport
- **Waxpeer input source**: 4th marketplace, free API, returns float values, often cheapest prices
- **Skin-targeted DMarket fetch**: fetches by exact skin name per collection+rarity instead of weapon type, so rare/expensive skins are not crowded out
- **Deduplication by listing_id** to prevent counting same listing multiple times
- **Expanded float filter**: Accepts FN, MW, and FT output conditions (not just MW). Calculates most permissive float limit per collection.
- Dynamic float limits per collection based on output skin float ranges
- Groups by collection, requires 10+ inputs of same rarity (liquidity check)
- Separates StatTrak/non-StatTrak (can't mix in trade-ups)
- **Steam-first output pricing**: Output prices sourced from Steam (free) first, then Skinport fallback, CSFloat only as last resort. Saves CSFloat budget for inputs.
- **Per-source platform fees**: Steam 15%, Skinport 8%, CSFloat 2% — applied correctly based on price source
- **Steam median price verification**: ALL output prices verified against Steam median/lowest. Caps inflated CSFloat/Skinport prices to prevent fake EV.
- **CSFloat budget guardrail**: Reserves 10 requests, per-page budget check, never fully exhausts budget
- **Unverifiable EV detection**: Trade-ups with any output skin missing a price are excluded from profitable results and shown separately
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
- [x] **Fix CSFloat input pagination** — Fixed: price cursor got stuck on price clusters, now advances by 1 cent past clusters instead of breaking.
- [ ] **Sell on Skinport (output)** — Currently if CSFloat has no listing for an output it's dead. Enabling Skinport as a valid sell platform (with qty ≥ 2 guard, minus Skinport fee) would unlock many collections currently skipped as NO_CSFLOAT_LISTINGS. Second biggest quick win.
- [x] **Add Waxpeer as input source** — Done. Fetches by skin name, normalizes to same format as DMarket/CSFloat, 6h cache.
- [ ] **CSFloat budget priority** — Reserve CSFloat API budget for output pricing (where we sell). Deprioritize CSFloat input fetching when budget is tight.
- [ ] **Rarity expansion** — Support all rarity tiers more broadly
- [ ] **Quick execution solution** — Auto-buy inputs or one-click purchase flow

### Medium Priority
- [x] **Steam output price fallback** — Done. Steam is now the primary output price source (free, based on actual sales, minus 15% fee). Skinport is secondary fallback (minus 8% fee), CSFloat last resort.
- [ ] **CSFloat float-range targeted fetch** — CSFloat API accepts min_float/max_float params. Instead of fetching all listings and filtering, query exactly the float ranges needed per collection. More surgical, less waste, more relevant results.
- [ ] **Add CS.Money as input source** — API returns floats per listing, good coverage on mid-tier skins DMarket misses.
- [ ] **Add BitSkins as input source** — Has float data in API, less popular but adds coverage.
- [ ] **Add buy order support** — Place buy orders on DMarket/Skinport at target prices
- [ ] **Auto-refresh mode** — Run every X minutes and alert on new opportunities
- [ ] **Steam 14-day price trend** — Add price history as trend indicator

### Low Priority
- [ ] **Integrate Pricempire** — Needs new subscription
- [ ] **Filter by Steam volume** — Skip illiquid outputs (<5 sales/day)
- [ ] **StatTrak trade-up support** — Show ST opportunities separately

### Noted Bugs (from audit)
- [ ] **Verification uses stale output prices** — `verify_opportunity` falls back to `out["price_raw"]` from the saved opportunity when cache is empty, so a crashed output price is never caught during verification. Fix: fetch fresh CSFloat price during verify, or at least flag "unverified output price" in the output.
- [ ] **StatTrak Unicode mismatch in Skinport lookup** — CSFloat may use `™` differently than Skinport's `market_hash_name`, causing Skinport price comparison to silently fail for StatTrak items. Fix: normalize Unicode before lookup.
- [ ] **Winners.md logs duplicates across runs** — `append_winners_log` appends every profitable result every run with no dedup. Same trade-up logged N times if profitable across N runs. Fix: check if collection+rarity+date already logged before appending.

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
CSFLOAT_BUDGET_RESERVE = 10       # Keep 10 requests in reserve
CSFLOAT_NO_LISTING = -1           # Sentinel: confirmed no CSFloat listing
SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
# Global rate limiter: 4 req/s for all CSFloat requests
```

## Quick Start

```bash
cd "C:\Users\Namai\cs2-bot"
python ev_calculator.py
```

First run fetches fresh data (~2 min for output prices at 4 req/s). Subsequent runs within 3h use cache (~10s).
