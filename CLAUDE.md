# CS2 Trade-Up EV Calculator

Tools for finding profitable CS2 trade-up contracts by scanning market prices and calculating expected value.

## Files

| File | Purpose |
|------|---------|
| `ev_calculator.py` | Main tool. 3-phase scanner that finds profitable trade-ups |
| `dmarket_prices.py` | Standalone DMarket price fetcher for low-float FT skins |
| `pricempire_prices.py` | Pricempire API wrapper (subscription expired, not used) |
| `winners.md` | Persistent log of all profitable trade-ups found across runs |
| `price_cache.json` | Auto-generated cache for output prices (v2: per-key timestamps, source, volume) |
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
2. **CSFloat confirms no listing** (200 OK, empty results) → try Skinport as sell platform (qty ≥ 2 guard, 8% fee). If Skinport also unavailable, marked `NO_CSFLOAT_LISTINGS` and excluded from EV.
3. **CSFloat rate-limited/error** (429 or network fail) → Skinport fallback, but only if Skinport has **≥ 2 listings** (singleton listings are unreliable price signals — e.g. one seller listing at 10× market price)

**Why quantity ≥ 2 for Skinport fallback:** A single Skinport listing can be priced at anything (e.g. SSG 08 Orange Filigree MW had 1 listing at $1,365 vs real market price of ~$53). Multiple listings indicate a real market.

## CSFloat Rate Limiting

CSFloat enforces a request rate limit. The code uses:

- **Global `RateLimiter` class** (token bucket): enforces max **4 req/s** (250ms between requests) across all threads
- **Exponential backoff on 429**: retries at 5s → 10s → 20s → 40s delays
- **No caching of failed results**: if all retries fail (returns 0), the result is NOT written to cache — next run will retry fresh
- **`CSFLOAT_NO_LISTING = -1` sentinel**: distinguishes "confirmed no listing" from "rate-limited unknown"

## Current State (v3.9)

**Working:**
- Fetches from DMarket + CSFloat + Waxpeer (bulk, 200 pages) + Skinport (~23,800 total items)
- **Waxpeer bulk fetch**: Paginated bulk fetch (200 pages), gets ~5,000 items with floats. Much faster than per-skin search.
- **Skin-targeted DMarket fetch**: fetches by exact skin name per collection+rarity instead of weapon type, so rare/expensive skins are not crowded out
- **Deduplication by listing_id** to prevent counting same listing multiple times
- **Expanded float filter**: Accepts FN, MW, and FT output conditions (not just MW). Calculates most permissive float limit per collection.
- Dynamic float limits per collection based on output skin float ranges
- Groups by collection, requires 10+ inputs of same rarity (liquidity check)
- Separates StatTrak/non-StatTrak (can't mix in trade-ups)
- **Steam-first output pricing**: Output prices sourced from Steam (free, 1.5s/req with 429 retry) first, then Skinport fallback, CSFloat only as last resort.
- **Per-source platform fees**: Steam 15%, Skinport 8%, CSFloat 2% — applied correctly based on price source
- **Price source persistence**: `price_cache.json` stores both prices AND their source, so cached prices keep correct fee labels across runs
- **Per-key cache expiry (v2)**: Each cached price has its own `fetched_at` timestamp. Stale entries (3-6h) used as fallback; only truly expired entries re-fetched. No more all-or-nothing cache wipes.
- **Skinport singleton filter (outputs)**: Rejects Skinport output prices with qty=1 (unreliable)
- **Skinport 2× sanity check**: Rejects Skinport output price if > 2× DMarket or Steam reference for same skin
- **Skinport as output sell platform**: When CSFloat confirms no listing, Skinport used as sell platform (qty ≥ 2 guard, 8% fee). Unlocks collections previously dead.
- **Multi-key CSFloat**: Supports multiple API keys via `CSFLOAT_API_KEYS` env var (comma-separated). Round-robin rotation with 60s cooldown on 429. Budget scales dynamically: N keys × 200 = total (50% inputs, 45% outputs, 5% reserve). Falls back to single `CSFLOAT_API_KEY`.
- **Two-pass EV system**: Pass 1 (broad scan) evaluates ALL single + multi-collection trade-ups using only cached/free data (zero API calls). Pass 2 (deep verify) uses CSFloat-primary verification on top candidates. CSFloat budget focused on highest-ROI outputs.
- **Waxpeer targeted fetch (Phase 1b)**: After initial input fetch, queries Waxpeer by exact skin name for near-viable (5-9 input) collections. Promotes watchlist collections to viable.
- **Unverifiable EV detection**: Trade-ups with any output skin missing a price are excluded from profitable results and shown separately
- **UNVERIFIED ON CSFLOAT warning**: Trade-ups where all output prices come from Steam/Skinport (no CSFloat verification) are flagged in results
- **Float violation hard skip**: Trade-ups where inputs exceed MaxFloat are skipped with ERROR log, not silently included
- **Steam 429 retry**: `fetch_steam_trend` retries 3× with 5s/15s/30s backoff, bails after 6 total 429s
- **StatTrak Unicode normalization**: NFKC normalization on both `extract_skin_name()` and `get_skinport_price()` for consistent StatTrak™ matching
- **Liquidity-weighted EV**: Output prices discounted by Steam 24h trading volume (100+/day=1.0, 10+=0.90, 2+=0.70, <2=0.50, unknown=0.85). Prevents recommending illiquid trade-ups.
- **Fee-aware sell platform recommendation**: Each output shows net proceeds on CSFloat/Skinport/Steam, ranked best to worst
- **Reverse search (Phase 0)**: Scans $5+ outputs top-down, works backwards to identify priority collections. Uses only cached data, zero API calls.
- **Multi-collection trade-ups**: Evaluates 4,500+ target+filler combinations using 3 strategies (cheapest, best EV/dollar, max jackpot). Max 4 collections per trade-up. Pre-computes ev_per_input and max_single_output for efficient scoring.
- **Winners dedup**: `append_winners_log` checks for existing entries by collection+rarity+date before appending
- **Multi-source verification**: When inputs are sold, replacement search checks DMarket + Waxpeer cache + CSFloat input cache
- ROI filter: only shows 25%+ ROI AND $0.30+ EV trade-ups — **do not lower this threshold**
- WATCH LIST: Shows collections with 5-9 inputs (close to executable)
- **Opportunity tracking:** Saves profitable trade-ups with exact listing IDs to `opportunities_cache.json`
- **Verification on startup:** Checks if saved listings still exist and prices remain profitable (with liquidity-adjusted EV)
- **Winners log:** Appends all profitable results to `winners.md` with date, ROI, EV, buy links (deduped)
- Caching: 6h for inputs, 3h for output prices (per-key timestamps, with source + volume tracking)

## TODO

### High Priority
- [x] **Add CSFloat as input source** — Done
- [x] **Skin-targeted DMarket fetch** — Done (fetches by exact skin name, not weapon type)
- [x] **Parallel output price fetching** — Done (5 workers, global rate limiter)
- [x] **CSFloat rate limit handling** — Done (token bucket + exponential backoff)
- [x] **Skinport output fallback** — Done (qty ≥ 2 guard, NO_CSFLOAT_LISTINGS exclusion)
- [x] **Fix CSFloat input pagination** — Fixed: price cursor got stuck on price clusters, now advances by 1 cent past clusters instead of breaking.
- [x] **Add Waxpeer as input source** — Done. Bulk fetch (200 pages), ~4,600 items with floats, 6h cache.
- [x] **CSFloat budget split** — Done. 140 inputs / 50 outputs / 10 reserve = 200 total. Smart output allocation pre-scans EV before spending budget.
- [x] **Steam 429 retry** — Done. 3 retries with 5s/10s/15s backoff, 1.5s between requests.
- [x] **Price source persistence** — Done. Cache stores source alongside price, correct fees applied across runs.
- [x] **Multi-collection trade-ups** — Done. 3 filler strategies (cheapest, best EV/dollar, max jackpot), evaluates 4,500+ combinations, max 4 collections per trade-up. Pre-computes ev_per_input for efficient scoring.
- [x] **Sell on Skinport (output)** — Done. When CSFloat has no listing, falls back to Skinport (qty ≥ 2 guard, 8% fee). Unlocks previously dead collections.
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

### Noted Bugs (from audit v3.7)
- [x] **Verification uses wrong fee for output prices** — `verify_opportunity` applied CSFLOAT_SELLER_FEE (2%) to ALL output prices regardless of source. Steam prices (15% fee) and Skinport prices (8% fee) were over-counted. Fixed: now uses `price_source` from saved opportunity.
- [x] **Waxpeer price unit mismatch** — Waxpeer API returns prices in millicents (1$ = 1000), but code treated them as cents. All Waxpeer prices were 10x inflated, making Waxpeer items never get selected. Fixed: divide by 10 to convert to cents.
- [x] **DMarket `fetch_skin_raw` missing source field** — Items from `fetch_skin_raw` had no `"source"` key, causing replacement inputs during verification to lose source tracking. Fixed: added `"source": "DMarket"` to all items.
- [x] **Dead assertion code** — Float violation check after `continue` was unreachable. Fixed: moved counter before `continue`.
- [x] **`_best_source` tracked listing source, not price source** — When Skinport had a cheaper price, `_best_source` still showed DMarket/CSFloat, misleading buy links and source tracking. Fixed: now tracks actual price source separately from listing source.
- [x] **`get_skinport_price` returned mixed types** — Returned string `"ERROR"` on miss vs dict on hit. Fixed: returns `None` on miss.
- [x] **CSFloat input fetch used adjusted float as raw float bound** — `max_adjusted` (0-1 normalized) was used directly as `max_float` server-side filter parameter, which expects raw float. Could under-fetch valid items for skins with narrow float ranges. Fixed: widened cap to 0.45 with comment explaining the conversion.
- [x] **StatTrak Unicode mismatch in Skinport lookup** — Fixed: NFKC normalization in both `extract_skin_name()` and `get_skinport_price()`.
- [x] **Winners.md logs duplicates across runs** — Fixed: dedup by collection+rarity+date before appending.
- [x] **Verification re-fetch only uses DMarket** — Fixed: now also searches Waxpeer cache and CSFloat input cache for replacements.
- [x] **Cache invalidation is all-or-nothing** — Fixed: v2 per-key cache format with individual `fetched_at` timestamps. Stale entries used as fallback.
- [x] **Phase 2 pre-scan vs actual EV mismatch** — Fixed: pre-scan now includes WW/BS outputs (with their cached price, usually 0) matching actual calc.

## Filler System Architecture (Multi-Collection Trade-Ups)

### How CS2 Multi-Collection Trade-Ups Work
- You can mix inputs from ANY collections, as long as all 10 are the same rarity
- Each input contributes to its own collection's output pool
- Output probability = `(inputs_from_collection / 10) * (1 / output_skins_in_collection)`
- Example: 7 inputs from Collection A (3 outputs) + 3 "fillers" from Collection B (2 outputs):
  - Each Collection A output: 7/10 × 1/3 = 23.3% chance
  - Each Collection B output: 3/10 × 1/2 = 15% chance

### Implemented Filler Algorithm (Phase 2b)
1. After Phase 1, builds **global pool** of all non-StatTrak inputs by rarity across all collections
2. Pre-computes `ev_per_input[(coll, rarity)]` and `max_single_output[(coll, rarity)]` for scoring
3. For each target collection with outputs at next rarity:
   - Tries all split ratios (1 to max_available target inputs)
   - 3 filler strategies per ratio: cheapest inputs, best EV/dollar, max jackpot
   - Max 4 collections per trade-up, dedup by listing ID set
   - Early pruning: skip if `target_ev_contribution < target_cost * 0.5` for 5+ inputs
4. Evaluates 4,500+ combinations per run (was ~32 before overhaul)
5. Probability per output: `(inputs_from_coll / 10) * (1 / outputs_in_coll)`

### Ongoing
- [ ] **Bug fixes** — Monitor and fix issues as they arise

## Improvement Roadmap (March 2026)

### Tier 1 — Highest impact, directly unlocks new profits

1. ~~**Multi-collection filler system**~~ — **DONE (v3.8).** 3 filler strategies, 4,500+ combos evaluated, max 4 collections. Found 8 profitable multi-collection trade-ups on first run.

2. ~~**Reverse search — start from valuable outputs**~~ — **DONE (v3.8).** `phase0_reverse_search()` ranks $5+ outputs, works backwards to priority collections. Zero API calls (cached data only).

3. **Steam sales history for output pricing** — Steam has an endpoint returning actual completed sales (not listings). Median of last 7 days of real sales is far more reliable than any listing price. Listings can be set to anything — sales reflect what people actually pay. Would be the gold standard for output pricing and largely eliminate inflated price problems.

### Tier 2 — Significant improvement, moderate effort

4. **Dynamic float optimization** — The bot picks 10 cheapest inputs below float threshold. But lower floats → better output condition → higher value. There's an optimal float point: 10 inputs at 0.152 might produce low MW ($8 sell) but cost $0.50 each, while 10 at 0.25 produce FT ($2 sell) at $0.02 each. Calculate EV at multiple float points and pick the one maximizing net profit, not just minimizing input cost.

5. ~~**Per-key cache expiry**~~ — **DONE (v3.8).** v2 cache format with per-entry `fetched_at` timestamps. Stale entries (3-6h) used as fallback; only expired entries re-fetched.

6. ~~**Liquidity-weighted EV**~~ — **DONE (v3.8).** `liquidity_multiplier()` discounts output prices by Steam 24h volume. Volume persisted in cache. Display shows volume/day rating per output.

7. ~~**Fee-aware sell platform recommendation**~~ — **DONE (v3.8).** Each output shows `SELL ON: CSFloat $X > Skinport $X > Steam $X` ranked by net proceeds.

### Tier 3 — Good improvements, lower urgency

8. **Real-time listing monitoring (WebSocket)** — DMarket and CSFloat have WebSocket/SSE feeds for new listings. Instead of batch scanning every few hours, subscribe and trigger EV calculation instantly when a cheap input appears. Profitable trade-ups exist for minutes, not hours.

9. **Alert system (Discord/Telegram)** — Push notifications when profitable trade-ups found: collection, ROI, EV, direct buy links, countdown estimate. No point finding opportunities if not at the computer.

10. **Buy order placement** — Place buy orders at target prices on DMarket/Skinport instead of buying at current listings. Passive approach: define the input price that makes the trade-up profitable, wait for sellers. Guaranteed profitability if orders fill.

11. **Float-targeted API fetching** — CSFloat and DMarket accept min_float/max_float params. Instead of fetching 70k items and filtering to 50k, query only the float ranges needed per collection. Cut Phase 1 from 70k to ~10k items.

12. **Historical tracking & model calibration** — Log every recommended trade-up and actual outcome. Did inputs cost what was predicted? Did outputs sell at predicted price? Real ROI vs predicted? Reveals systematic biases (e.g., "Skinport output prices are consistently 15% above actual sell").

### Tier 4 — Speculative / longer-term

13. **Cross-marketplace arbitrage** — Same skin priced differently across platforms (DMarket $0.50, CSFloat $1.20) is direct arbitrage, no trade-up needed. Data is already fetched — just add a comparison pass.

14. **Portfolio/bankroll optimization** — With a $50 budget, which combination of trade-ups maximizes expected return? Kelly criterion for bet sizing. Don't put everything into one trade-up.

15. **Pattern/sticker premium awareness** — Some outputs have patterns worth 10-100x (e.g., Case Hardened blue gems). Can't predict which pattern, but flag "this collection has pattern-premium outputs" as upside.

16. **Buff163 integration** — Biggest CS2 marketplace globally (Chinese market), often cheapest. API access requires Chinese phone verification but would significantly expand input coverage.

## Key Constants

```python
SKINPORT_COOLDOWN = 60            # 1 minute between Skinport API calls
CACHE_EXPIRY = 3 * 60 * 60        # 3 hours fresh (output prices)
INPUT_CACHE_EXPIRY = 6 * 60 * 60  # 6 hours fresh (input listings)
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours stale fallback
MIN_ROI = 25.0                    # Only show 25%+ ROI — do not lower
MIN_EV = 30                       # Only show $0.30+ net profit (in cents)
CSFLOAT_SELLER_FEE = 0.02         # 2% when selling on CSFloat
# Dynamic budget: N keys × 200 total, split 50/45/5%
CSFLOAT_BUDGET_RESERVE = max(10, total * 0.05)
CSFLOAT_INPUT_CAP = max(140, total * 0.50)
CSFLOAT_OUTPUT_RESERVE = max(50, total * 0.45)
CSFLOAT_NO_LISTING = -1           # Sentinel: confirmed no CSFloat listing
SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
MAX_COLLECTIONS = 4               # Max collections in multi-collection trade-up
# Steam: 1.5s between requests, 3 retries on 429 with 5s/15s/30s backoff, bail at 6 total 429s
# CSFloat: 4 req/s global rate limiter
# Liquidity multiplier: 100+/day=1.0, 10+=0.90, 2+=0.70, <2=0.50, unknown=0.85
```

## Quick Start

```bash
cd "C:\Users\oskar\Desktop\Claude-code"
python ev_calculator.py
```

First run fetches fresh data (~20 min for Steam output prices due to rate limiting). Subsequent runs within 3h use per-key cache (~10s). Cache v2 format means only stale entries are re-fetched, not all 700+.
