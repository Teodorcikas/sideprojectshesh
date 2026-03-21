# CS2 Trade-Up EV Calculator

Tools for finding profitable CS2 trade-up contracts by scanning market prices and calculating expected value.

## Files

| File | Purpose |
|------|---------|
| `ev_calculator.py` | Main tool. 3-phase scanner that finds profitable trade-ups |
| `dmarket_prices.py` | Standalone DMarket price fetcher for low-float FT skins |
| `pricempire_prices.py` | Pricempire API wrapper (subscription expired, not used) |
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
| **Skinport** | Input prices (often cheaper than DMarket) | None, but rate limited | Working (60s cooldown) |
| **CSFloat** | Output prices (where we sell) | API key in file | Working |
| **Steam Market** | Volume/trend data for outputs | None | Working |
| **CSGO-API** (GitHub) | Skin database with float ranges | None | Working |
| **Pricempire** | Alternative pricing | API key expired | Not used |

## Trade-Up Strategy

**Core approach:** Buy 10 low-float Field-Tested skins, trade up to guaranteed Minimal Wear outputs.

1. Find FT skins with float near 0.15 (FT minimum)
2. Calculate max input float per collection: `max_input = (0.15 - out_min) / (out_max - out_min)`
3. Output float formula: `output_float = avg_input * (max - min) + min`
4. MW threshold is 0.15 — inputs must be low enough to guarantee MW outputs
5. Profit when: `expected_output_value > input_cost + fees`

**Fees:**
- CSFloat seller fee: 2%
- Steam seller fee: 15% (for comparison only)
- No buyer fees on DMarket/Skinport

## Current State (v3.2)

**Working:**
- Fetches from DMarket (7000+ items) + CSFloat (2000 items via price-based pagination)
- **Deduplication by listing_id** to prevent counting same listing multiple times
- Dynamic float limits per collection based on output skin ranges
- Groups by collection, requires 10+ inputs of same rarity (liquidity check)
- Separates StatTrak/non-StatTrak (can't mix in trade-ups)
- CSFloat output prices with 2% fee calculation
- Steam volume/trend data for profitable trade-ups only
- ROI filter: only shows 25%+ ROI AND $0.30+ EV trade-ups
- WATCH LIST: Shows collections with 5-9 inputs (close to executable)
- **Opportunity tracking:** Saves profitable trade-ups with exact listing IDs to `opportunities_cache.json`
- **Verification on startup:** Checks if saved listings still exist and prices remain profitable
- Caching: 6h for inputs, 3h for output prices
- Skinport rate limiter: 60s cooldown between API calls

## TODO

### High Priority
- [x] **Add CSFloat as input source** — Done! Fetches low-float FT listings
- [ ] **Steam 14-day price trend** — Add Steam price history (last 14 days) as a price source/trend indicator
- [ ] **Lower ROI threshold option** — Add CLI arg to show 10%+ or 15%+ ROI for more visibility

### Medium Priority
- [ ] **Fetch more items** — Increase beyond 2000/weapon or add pagination for better coverage
- [ ] **Add buy order support** — Place buy orders on DMarket/Skinport at target prices
- [ ] **Historical EV tracking** — Log profitable opportunities over time
- [ ] **Auto-refresh mode** — Run every X minutes and alert on new opportunities

### Low Priority
- [ ] **Integrate Pricempire** — Needs new subscription
- [ ] **Filter by Steam volume** — Skip illiquid outputs (<5 sales/day)
- [ ] **StatTrak trade-up support** — Currently filters to non-ST, could show ST opportunities separately

## Key Constants

```python
SKINPORT_COOLDOWN = 60            # 1 minute between Skinport API calls
CACHE_EXPIRY = 3 * 60 * 60        # 3 hours fresh (output prices)
INPUT_CACHE_EXPIRY = 6 * 60 * 60  # 6 hours fresh (input listings)
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours stale fallback
MIN_ROI = 25.0                    # Only show 25%+ ROI
MIN_EV = 30                       # Only show $0.30+ net profit (in cents)
CSFLOAT_SELLER_FEE = 0.02         # 2% when selling on CSFloat
```

## Quick Start

```bash
cd ~/Desktop/Claude-code
python ev_calculator.py
```

First run fetches fresh data (~2-3 min). Subsequent runs within 3h use cache (~30s).
