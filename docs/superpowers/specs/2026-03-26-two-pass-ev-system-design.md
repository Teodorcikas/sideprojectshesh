# Two-Pass EV System + Multi-Key CSFloat + Better Waxpeer Coverage

## Context

The bot (v3.8) finds too few opportunities because output price coverage is incomplete. This morning's run: 8 profitable results, all involving Kilowatt — the one collection with good price data. Root cause: Steam gets rate-limited (429'd) after ~350 requests, leaving hundreds of output skins unpriced. The CSFloat budget (50 outputs) is too small to compensate. Result: most collections are invisible.

**Goal:** Find significantly more profitable trade-ups per run by:
1. Increasing CSFloat output budget via multiple API keys (1,000-2,000 total)
2. Improving Waxpeer input coverage (currently only 26% yield on float data)
3. Restructuring EV calculation into a two-pass system: broad scan first (free data), then targeted verification (CSFloat-primary)

**Non-goals:** Real-time monitoring, alerting, new marketplace integrations (BitSkins, Buff163, CS.Money). Batch model stays.

---

## Part 1: Multi-Key CSFloat System

### Problem
Single API key = 200 request budget (140 inputs, 50 outputs, 10 reserve). With 700+ output skins needing prices, only 50 get CSFloat-verified. The rest rely on Skinport (8% fee, less reliable).

### Design

**Configuration:**
- New env var `CSFLOAT_API_KEYS` — comma-separated list of API keys
- Falls back to existing single `CSFLOAT_API_KEY` if `CSFLOAT_API_KEYS` not set
- Each key has 200 request budget per day (assumed, to be validated)

**Key Rotation:**
- `MultiKeyRateLimiter` class wraps N `RateLimiter` instances (one per key)
- Round-robin key selection: `key_index = request_count % num_keys`
- If a key gets 429'd, mark it as cooling down (skip for 60s), advance to next key
- Each key maintains its own 4 req/s token bucket
- Effective throughput: N × 4 req/s (e.g., 5 keys = 20 req/s)

**Budget Tracking:**
- Total budget: `num_keys × 200`
- Split proportionally: ~50% inputs, ~45% outputs, ~5% reserve
- Example with 5 keys (1,000 total): 500 inputs / 450 outputs / 50 reserve

**Code Changes:**
- File: `ev_calculator.py`
- Replace global `_csfloat_rate_limiter` (single RateLimiter) with `MultiKeyRateLimiter`
- `fetch_csfloat_price(name, cond)` and CSFloat input fetch functions get key from the multi-key limiter
- Constants `CSFLOAT_INPUT_CAP`, `CSFLOAT_OUTPUT_RESERVE`, `CSFLOAT_BUDGET_RESERVE` become computed from total budget
- Existing `_csfloat_budget` counter becomes per-key counters summed for total

---

## Part 2: Improved Waxpeer Input Coverage

### Problem
Bulk fetch (200 pages) yields ~19,000 items but only ~5,000 have float data (26%). 14,000+ items are wasted. This means many collections stay below the 10-input viability threshold.

### Design

**Phase 1a: Bulk fetch (existing)**
- Keep the current 200-page bulk fetch as baseline
- Still yields ~5,000 items with floats across all collections

**Phase 1b: Targeted follow-up (new)**
- After Phase 1 grouping, identify "near-viable" collections: 5-9 inputs at any rarity
- For each near-viable collection, get the skin names that belong to it at the needed rarity
- Query Waxpeer's search-by-name endpoint for those specific skins (needs API verification — if no search endpoint exists, use filtered bulk fetch with name parameter)
- Search-by-name typically has better float data yield than bulk pagination
- Goal: convert watchlist (5-9 inputs) collections to viable (10+)

**Implementation:**
- New function: `waxpeer_targeted_fetch(near_viable_collections, coll_skins, skin_float_ranges)`
- Called after initial Phase 1 grouping, before Phase 2
- Uses same Waxpeer API key, same cache
- Estimated additional requests: ~50-150 (one per skin name in near-viable collections)
- Results merged into existing input pool, deduplication by listing_id applies

**Budget:** Waxpeer API is free with key. No rate limit concerns at this volume.

---

## Part 3: Two-Pass EV Calculation

### Problem
Phase 2 (single-coll) and Phase 2b (multi-coll) fetch output prices independently, competing for rate-limited Steam/CSFloat budget. Steam gets blocked early, leaving Phase 2b blind. Output price fetching is spread across ALL 700+ outputs equally instead of focused on promising trade-ups.

### Design

**Structural change:** Merge Phase 2 and Phase 2b into a unified two-pass pipeline.

### Pass 1 — Broad Scan (zero API calls)

**Purpose:** Screen ALL possible trade-ups using only free/pre-fetched data. Identify which ones look promising enough to verify.

**Data sources (all already available, no API calls):**
- Skinport bulk prices (23,855 items, already cached from Phase 1)
- DMarket reference prices (from input fetch cache)
- Price cache from prior runs (per-key timestamps, may have hours-old but valid prices)

**Filter relaxation for Pass 1:**
- Skinport singleton filter: ALLOW qty=1 (flag as `low_confidence`, don't reject)
- Skinport sanity check: WARN at >2× reference instead of REJECT
- No liquidity discount applied yet (that's a Pass 2 concern)

**EV calculation:**
- For each output skin, use the best available price from: cache (any source) > Skinport > DMarket ref
- Apply the source's fee (cached source fee, or Skinport 8%, or DMarket as estimate)
- Calculate "optimistic EV" and "optimistic ROI" for:
  - All single-collection trade-ups (current Phase 2 logic)
  - All multi-collection combinations (current Phase 2b logic, 4,500+ combos)
- Rank all trade-ups by optimistic EV descending

**Output:** Ranked list of ~50-200 candidate trade-ups with optimistic EV, flagged confidence levels per output price.

### Pass 2 — Deep Verify (targeted CSFloat + Skinport)

**Purpose:** Verify output prices for the most promising candidates from Pass 1 using CSFloat as primary authority.

**Candidate selection:**
- Collect unique output skins from Pass 1 candidates, ordered by the highest-ROI trade-up that needs them
- Verify up to `output_budget` unique skins (e.g., 450 with 5 keys)
- In practice: ~150 trade-ups share ~200-300 unique output skins, so 450 budget covers most/all candidates
- If budget is tight (single key, 50 outputs): prioritize by optimistic ROI descending, verify only top ~20 trade-ups' outputs

**Price verification (CSFloat-primary):**
- For each output skin needing verification:
  1. **CSFloat has listing** -> use CSFloat price (2% fee). Done.
  2. **CSFloat confirms no listing** (200 OK, empty) -> `NO_CSFLOAT_LISTINGS`. Skin excluded from EV. No fallback.
  3. **CSFloat rate-limited/error** -> Use Skinport fallback (qty >= 2 guard, 8% fee)
- **No Steam fallback for output pricing.** CSFloat is the authority — if it has no listing, the skin is effectively unsellable on third-party platforms.

**Steam usage (limited):**
- Steam is ONLY used for liquidity data (`volume_24h`) on skins that HAVE a CSFloat/Skinport price
- Fetched only for Pass 2 candidates, not all outputs
- Budget: ~50-100 Steam calls (much less than current 412), manageable within rate limits

**Strict filters applied in Pass 2:**
- Skinport singleton check: reject qty=1 for final EV (overrides Pass 1 relaxation)
- Skinport 2x sanity check: reject if >2x CSFloat price for same skin
- Liquidity multiplier: applied based on Steam volume_24h (or 0.85 default if no volume data)

**EV re-calculation:**
- For each candidate trade-up, re-calculate EV with verified prices
- Apply all strict filters
- Trade-ups that still meet 25% ROI + $0.30 EV threshold -> profitable results
- Trade-ups with any missing output price -> "unverifiable" bucket (shown separately)

**Output:** Final profitable trade-ups with verified prices, buy/sell links, confidence labels.

### Unified Pipeline Flow

```
Phase 0: Reverse search (unchanged, cached data only)
Phase 1: Fetch inputs (DMarket + CSFloat + Waxpeer bulk + Skinport)
Phase 1b: Waxpeer targeted fetch for near-viable collections (NEW)
Phase 2 Pass 1: Broad scan — price all outputs from free data, calculate optimistic EV, rank
Phase 2 Pass 2: Deep verify — CSFloat-primary verification of top N candidates, re-calculate verified EV
Phase 3: Fetch Steam trends for profitable trade-ups (unchanged)
Verification: Check listings still exist (unchanged)
Output: Results, winners.md, opportunities_cache.json
```

---

## Changes to Output Display

- Pass 1 results shown as "SCREENING RESULTS" with count (e.g., "142 candidates identified")
- Pass 2 results shown as "VERIFIED RESULTS" — these are the real profitable trade-ups
- Each output in final results shows: `Price [CSFloat -2%]: $X.XX` or `Price [Skinport -8%]: $X.XX (CSFloat rate-limited)`
- `!! UNVERIFIED ON CSFLOAT !!` flag only applies when CSFloat was rate-limited, never for intentional exclusion

---

## Files Modified

| File | Changes |
|------|---------|
| `ev_calculator.py` | Multi-key rate limiter, Waxpeer targeted fetch, two-pass EV pipeline |

Single file change — all logic lives in `ev_calculator.py`.

---

## Future TODO (not in this spec)

- **Steam sales history endpoint** — Use `pricehistory` for completed transaction data instead of listing prices. Would improve Pass 1 screening accuracy. Lower priority now that CSFloat budget is large.
- **Steam rate limit improvements** — Smarter pacing, session rotation. Deferred since Steam is no longer primary output source.

---

## Verification Plan

1. **Run with current single key first** — confirm two-pass pipeline works with existing 200 budget before adding keys
2. **Compare results:** Run old code and new code on same cache data. New code should find >= same opportunities plus additional ones.
3. **Check Pass 1 -> Pass 2 funnel:** Log how many Pass 1 candidates survive Pass 2 verification. If < 10%, Pass 1 screening is too loose. If > 80%, it's too strict (not screening enough).
4. **Budget utilization:** Log CSFloat requests used (inputs vs outputs). Should see output budget fully utilized, not wasted on low-value skins.
5. **Waxpeer targeted fetch:** Log how many watchlist collections got promoted to viable after targeted fetch.
6. **End-to-end:** Run full pipeline, check winners.md for new collections that weren't found before.
