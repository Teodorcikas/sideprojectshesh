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

## Current State (v3.7)

**Working:**
- Fetches from DMarket + CSFloat + Waxpeer (bulk, 200 pages) + Skinport (~19,500 total items)
- **Waxpeer bulk fetch**: Paginated bulk fetch (200 pages), gets ~4,600 items with floats. Much faster than per-skin search.
- **Skin-targeted DMarket fetch**: fetches by exact skin name per collection+rarity instead of weapon type, so rare/expensive skins are not crowded out
- **Deduplication by listing_id** to prevent counting same listing multiple times
- **Expanded float filter**: Accepts FN, MW, and FT output conditions (not just MW). Calculates most permissive float limit per collection.
- Dynamic float limits per collection based on output skin float ranges
- Groups by collection, requires 10+ inputs of same rarity (liquidity check)
- Separates StatTrak/non-StatTrak (can't mix in trade-ups)
- **Steam-first output pricing**: Output prices sourced from Steam (free, 1.5s/req with 429 retry) first, then Skinport fallback, CSFloat only as last resort.
- **Per-source platform fees**: Steam 15%, Skinport 8%, CSFloat 2% — applied correctly based on price source
- **Price source persistence**: `price_cache.json` stores both prices AND their source, so cached prices keep correct fee labels across runs
- **Skinport singleton filter (outputs)**: Rejects Skinport output prices with qty=1 (unreliable)
- **Skinport 3× sanity check**: Rejects Skinport output price if > 3× Steam median for same skin
- **CSFloat budget split**: 150 inputs / 50 outputs / 10 reserve. Smart output allocation only fetches CSFloat for promising trade-ups (EV pre-scan).
- **Unverifiable EV detection**: Trade-ups with any output skin missing a price are excluded from profitable results and shown separately
- **UNVERIFIED ON CSFLOAT warning**: Trade-ups where all output prices come from Steam/Skinport (no CSFloat verification) are flagged in results
- **Float violation hard skip**: Trade-ups where inputs exceed MaxFloat are skipped with ERROR log, not silently included
- **Steam 429 retry**: `fetch_steam_trend` retries 3× with 5s/10s/15s backoff on rate limit
- ROI filter: only shows 25%+ ROI AND $0.30+ EV trade-ups — **do not lower this threshold**
- WATCH LIST: Shows collections with 5-9 inputs (close to executable)
- **Opportunity tracking:** Saves profitable trade-ups with exact listing IDs to `opportunities_cache.json`
- **Verification on startup:** Checks if saved listings still exist and prices remain profitable
- **Winners log:** Appends all profitable results to `winners.md` with date, ROI, EV, buy links
- Caching: 6h for inputs, 3h for output prices (with source tracking)

## TODO

### High Priority
- [x] **Add CSFloat as input source** — Done
- [x] **Skin-targeted DMarket fetch** — Done (fetches by exact skin name, not weapon type)
- [x] **Parallel output price fetching** — Done (5 workers, global rate limiter)
- [x] **CSFloat rate limit handling** — Done (token bucket + exponential backoff)
- [x] **Skinport output fallback** — Done (qty ≥ 2 guard, NO_CSFLOAT_LISTINGS exclusion)
- [x] **Fix CSFloat input pagination** — Fixed: price cursor got stuck on price clusters, now advances by 1 cent past clusters instead of breaking.
- [x] **Add Waxpeer as input source** — Done. Bulk fetch (200 pages), ~4,600 items with floats, 6h cache.
- [x] **CSFloat budget split** — Done. 150 inputs / 50 outputs / 10 reserve. Smart output allocation pre-scans EV before spending budget.
- [x] **Steam 429 retry** — Done. 3 retries with 5s/10s/15s backoff, 1.5s between requests.
- [x] **Price source persistence** — Done. Cache stores source alongside price, correct fees applied across runs.
- [ ] **Multi-collection trade-ups** — Currently only analyzes trade-ups within a single collection. CS2 trade-ups can mix inputs from ANY collections of the same rarity. Supporting multi-collection trade-ups would massively expand the number of possible profitable combinations. Biggest win for finding new opportunities.
- [ ] **Sell on Skinport (output)** — Currently if CSFloat has no listing for an output it's dead. Enabling Skinport as a valid sell platform (with qty ≥ 2 guard, minus Skinport fee) would unlock many collections currently skipped as NO_CSFLOAT_LISTINGS.
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
- [ ] **StatTrak Unicode mismatch in Skinport lookup** — CSFloat may use `™` differently than Skinport's `market_hash_name`, causing Skinport price comparison to silently fail for StatTrak items. Fix: normalize Unicode before lookup.
- [ ] **Winners.md logs duplicates across runs** — `append_winners_log` appends every profitable result every run with no dedup. Same trade-up logged N times if profitable across N runs. Fix: check if collection+rarity+date already logged before appending.
- [ ] **Verification re-fetch only uses DMarket** — When inputs are sold, replacement search only queries DMarket, missing potentially cheaper CSFloat/Waxpeer alternatives.
- [ ] **Cache invalidation is all-or-nothing** — Output price cache uses a single timestamp. At 3h01m ALL cached prices are wiped, forcing full re-fetch. Should use per-key timestamps.
- [ ] **Phase 2 pre-scan vs actual EV mismatch** — Pre-scan skips WW/BS outputs from EV but actual calculation includes them (with price=0), potentially inflating the pre-scan's estimate of which trade-ups are "promising".

## Filler System Architecture (Multi-Collection Trade-Ups)

### How CS2 Multi-Collection Trade-Ups Work
- You can mix inputs from ANY collections, as long as all 10 are the same rarity
- Each input contributes to its own collection's output pool
- Output probability = `(inputs_from_collection / 10) * (1 / output_skins_in_collection)`
- Example: 7 inputs from Collection A (3 outputs) + 3 "fillers" from Collection B (2 outputs):
  - Each Collection A output: 7/10 × 1/3 = 23.3% chance
  - Each Collection B output: 3/10 × 1/2 = 15% chance

### Current Code Blockers for Filler System
1. **`phase1_fetch_inputs`** groups by `(collection, rarity)` and requires 10+ from same collection. Must pool ALL inputs of same rarity across collections.
2. **`phase2_calculate_ev`** iterates `viable_collections[coll_name]` per collection. Must instead enumerate combinations of collections.
3. **Probability calculation** (`prob = 1 / len(outputs)`) assumes single collection. Must use weighted probability per collection.
4. **Output skin lookup** uses `coll_skins[coll_name].get(out_rarity)` for one collection. Must aggregate outputs from all contributing collections.
5. **Combinatorial explosion** — mixing N collections × M skins is exponential. Need heuristics: only combine collections that individually have <10 inputs but together reach 10+, or use one "target" collection with cheap fillers from another.

### Suggested Architecture for Filler System
1. After Phase 1, build a **global pool** of all inputs by rarity (across all collections)
2. For each "target" collection with valuable outputs but <10 inputs:
   - Find cheapest fillers from OTHER collections at same rarity
   - Calculate the mixed-collection EV using weighted probabilities
   - Filler outputs are usually cheap (that's why fillers are cheap) — the value comes from target outputs
3. Optimize: sort target collections by potential output value, fill from cheapest available inputs

### Ongoing
- [ ] **Bug fixes** — Monitor and fix issues as they arise

## Improvement Roadmap (March 2026)

### Tier 1 — Highest impact, directly unlocks new profits

1. **Multi-collection filler system** — The single biggest limitation. 16 collections sit on watchlist with 5-9 inputs. With fillers, combine e.g., 7 inputs from a valuable collection + 3 cheap fillers from another. Key insight: don't just pick cheapest fillers — pick fillers whose collection ALSO has valuable outputs. 3 inputs from Collection B gives 30% × (1/num_outputs) chance at Collection B's outputs too. Optimize full probability-weighted EV across both collections, not just minimize filler cost.

2. **Reverse search — start from valuable outputs** — Instead of "find cheap inputs → calculate outputs → check profit", flip it: rank all output skins by value ($5+, $10+, $50+ MW/FN skins), work backwards to which collections produce them, check if inputs are available cheaply enough. Targets exactly where profit is possible, skips thousands of low-value collections.

3. **Steam sales history for output pricing** — Steam has an endpoint returning actual completed sales (not listings). Median of last 7 days of real sales is far more reliable than any listing price. Listings can be set to anything — sales reflect what people actually pay. Would be the gold standard for output pricing and largely eliminate inflated price problems.

### Tier 2 — Significant improvement, moderate effort

4. **Dynamic float optimization** — The bot picks 10 cheapest inputs below float threshold. But lower floats → better output condition → higher value. There's an optimal float point: 10 inputs at 0.152 might produce low MW ($8 sell) but cost $0.50 each, while 10 at 0.25 produce FT ($2 sell) at $0.02 each. Calculate EV at multiple float points and pick the one maximizing net profit, not just minimizing input cost.

5. **Per-key cache expiry** — Already noted as a bug. When price_cache hits 3h01m, ALL 700+ output prices wiped. Per-key timestamps mean most runs need 0-10 fresh fetches instead of 700. Massive speed improvement on typical runs.

6. **Liquidity-weighted EV** — A $50 output selling 2/day is risky. A $5 output selling 500/day is reliable. Discount EV by liquidity: >100/day = full EV, 10-100 = 90%, <10 = 70%, <2 = 50% or flag as risky. Prevents recommending trade-ups that look profitable but take weeks to sell.

7. **Fee-aware sell platform recommendation** — For each output, calculate net on each platform (CSFloat 2%, Skinport 8%, Steam 15%) and recommend the best one. Currently assumes you sell at the source's fee, but you should sell on the cheapest-fee platform with buyers.

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
CSFLOAT_BUDGET_RESERVE = 10       # Keep 10 requests in reserve
CSFLOAT_INPUT_CAP = 150           # Max requests for input fetching
CSFLOAT_OUTPUT_RESERVE = 50       # Reserved for output price verification
CSFLOAT_NO_LISTING = -1           # Sentinel: confirmed no CSFloat listing
SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
# Steam: 1.5s between requests, 3 retries on 429 with 5s/10s/15s backoff
# CSFloat: 4 req/s global rate limiter
```

## Quick Start

```bash
cd "C:\Users\Namai\cs2-bot"
python ev_calculator.py
```

First run fetches fresh data (~2 min for output prices at 4 req/s). Subsequent runs within 3h use cache (~10s).
