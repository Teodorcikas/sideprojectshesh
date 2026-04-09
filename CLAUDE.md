# CS2 Trade-Up EV Calculator (v4.1)

Finds profitable CS2 trade-up contracts by scanning market prices and calculating expected value.

## Quick Start

```bash
cd "C:\Users\oskar\Desktop\Claude-code"
python ev_calculator.py
```

First run: ~5-10 min (Steam rate limiting). Cached runs: ~10s. Caches: 6h inputs, 3h output prices.

## Files

| File | Purpose |
|------|---------|
| `ev_calculator.py` | Main scanner (Phase 0-3 pipeline) |
| `winners.md` | Log of all profitable trade-ups found |
| `opportunities_cache.json` | Saved opportunities with listing IDs |
| `price_cache.json` | Output prices (v2: per-key timestamps, source, volume) |
| `skinport_cache.json` | Skinport prices (3h fresh, 6h stale) |
| `dmarket_cache.json` | DMarket listings (6h fresh) |
| `csfloat_input_cache.json` | CSFloat input listings (6h fresh) |
| `waxpeer_cache.json` | Waxpeer listings (6h fresh) |

## APIs

| API | Purpose | Rate Limit |
|-----|---------|------------|
| **DMarket** | Input listings with floats | None |
| **Skinport** | Input prices + output fallback | 60s cooldown |
| **CSFloat** | Output prices (sell platform) | 2 keys, 200 req/key, per-key budget tracking |
| **Waxpeer** | Input listings with floats | API key required |
| **Steam** | Volume/trend data + output pricing | 1.5s/req, bails after 4 cumulative 429s |
| **CSGO-API** | Skin database (live from GitHub) | None |

## Trade-Up Math

- Max input float per skin: `(target_output_float - out_min) / (out_max - out_min)`
- Output float: `avg_adjusted_input * (out_max - out_min) + out_min`
- Adjusted float: `(raw - skin_min) / (skin_max - skin_min)`
- Fees: CSFloat 2%, Skinport 8%, Steam 15%

## Pipeline

1. **Phase 0** — Reverse search: rank $5+ outputs, identify priority collections (cached data only)
2. **Phase 1** — Fetch inputs: DMarket (targeted by skin name) + CSFloat + Waxpeer (bulk 200pg) + Skinport prices
3. **Phase 1b** — Targeted Waxpeer fetch for watchlist (5-9 input) collections
4. **Phase 2 Pass 1** — Broad scan: evaluate all single + multi-collection trade-ups (zero API calls, cached data)
5. **Phase 2 Pass 2** — Deep verify: CSFloat-primary price verification for top candidates by ROI
6. **Phase 3** — Steam trends for profitable results, save opportunities, append winners

## Output Price Sanity

- **CSFloat singleton (1 listing)**: rejected if >2x any reference price (Steam/Skinport/DMarket), or if no reference exists on any platform
- **CSFloat 2+ listings**: warned but accepted if >3x reference
- **Skinport**: rejected if qty=1 or >2x DMarket/Steam reference
- **Fallback chain**: Steam (primary, free) -> Skinport (8% fee, qty>=2) -> CSFloat (2% fee)

## Multi-Collection Trade-Ups

- Mix inputs from ANY collections if same rarity; output probability = `(inputs_from_coll / 10) * (1 / outputs_in_coll)`
- 4 filler strategies: cheapest, best EV/dollar, max jackpot, lowest adjusted float
- Max 4 collections per trade-up, 4,500+ combinations evaluated

## Key Constants

```python
MIN_ROI = 25.0                    # Only show 25%+ ROI -- DO NOT LOWER
MIN_EV = 30                       # Only show $0.30+ net profit (cents)
CSFLOAT_SELLER_FEE = 0.02         # 2% sell fee
SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
MAX_COLLECTIONS = 4               # Max collections in multi-collection trade-up
# Per-key CSFloat budget: N keys x 200, split 50% inputs / 45% outputs / 5% reserve
# Steam: 1.5s between requests, bail after 4 cumulative 429s
# Liquidity multiplier: 100+/day=1.0, 10+=0.90, 2+=0.70, <2=0.50, unknown=0.90
```

## Known Limitations / TODO

### Bugs to fix
- [ ] **Watchlist collections still vanish after Phase 1b** — Despite merge-back code in main(), `Watchlist: 0 (was 12)` after Phase 1b even though only 2 were promoted. The `_classify_collections` rebuild on `merged_items` drops them and the restore loop doesn't catch them. Likely a dedup or collection-name-vs-rarity mismatch issue. Investigate why `coll not in viable_collections and coll not in watchlist_collections` fails for these 10 collections.
- [ ] **Exclude non-tradeable items** — Limited Edition and Souvenir items can't be used in trade-ups but are currently included
- [ ] **Multi-collection combo count log is wrong** — "361 combos" is single-collection only; real search space is combinatorial

### Next improvements
- [ ] **Steam sales history** — Use actual completed sales (median of 7 days) instead of listing prices. Gold standard for output pricing.
- [ ] **Dynamic float optimization** — Find optimal float point maximizing net profit, not just cheapest inputs
- [ ] **CSFloat float-range targeted fetch** — Query exact float ranges needed instead of fetching all and filtering
- [ ] **Real-time listing monitoring** — WebSocket feeds from DMarket/CSFloat for instant alerts
- [ ] **Alert system** — Discord/Telegram notifications for profitable trade-ups
- [ ] **Buy order placement** — Place orders at target prices for passive profitability

## Recent Fixes (v4.1, 2026-04-08)

- **StatTrak items recovered** — 11,000+ StatTrak items were silently dropped due to Unicode normalization turning `™` into `TM`. `extract_skin_name()` now handles `StatTrakTM` prefix. This should unlock StatTrak trade-ups.
- **Steam 429 bail actually works** — Counter no longer decrements on success; bails after 4 cumulative 429s instead of grinding forever
- **CSFloat singleton filter tightened** — Singletons >2x ref rejected (was 3x); singletons with no reference on any platform rejected entirely (was: silently accepted)
- **Per-key CSFloat budget** — Budget tracker now per-API-key (was single global, causing 2-key setups to bail at 199/400)
- **Item drop accounting** — `process_cached_items()` now logs all skip reasons (was: 14,825 items silently dropped)
- **Watchlist preserved after Phase 1b** — Collections no longer vanish from watchlist after targeted fetch
