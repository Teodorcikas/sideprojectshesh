# CSFloat Sanity Check + Filler Float Optimization Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two bugs — CSFloat output prices have no sanity check (producing fake profitable trade-ups like the $809 P90 Baroque Red), and multi-collection filler system produces 0 candidates because it can't price target collection outputs. Then improve filler selection to optimize for adjusted float reduction, not just cheapest price.

**Architecture:** Three independent fixes in `ev_calculator.py`: (1) Add a Steam/DMarket cross-reference sanity check for CSFloat prices in Pass 2, mirroring the existing Skinport 2x check. (2) Fix multi-collection candidate generation to work when target outputs lack cached prices by deferring pricing to Pass 2 verification. (3) Add a "low adjusted float" filler strategy that picks fillers with the lowest adjusted floats to push output conditions higher (e.g. FT→MW), dramatically increasing output value.

**Tech Stack:** Python 3, requests, existing `ev_calculator.py` codebase (~4500 lines)

---

### Task 1: Add CSFloat price sanity check in Pass 2

**Why this matters:** `fetch_csfloat_price` returns the cheapest buy-now listing. If there's only one listing at $809 for a $5 skin, that price is accepted and cached. Skinport has a 2x sanity check + singleton filter — CSFloat has neither. This produced the fake Canals +207% ROI result.

**Files:**
- Modify: `ev_calculator.py:2826-2850` (Pass 2 deep verify — where CSFloat prices are stored)
- Modify: `ev_calculator.py:1754-1809` (fetch_csfloat_price — add quantity info)

- [ ] **Step 1: Modify `fetch_csfloat_price` to return listing count alongside price**

Currently returns just `int(listings[0].get("price", 0))`. Change it to also fetch with `limit=5` and return `(price, count)` so the caller knows if it's a singleton.

In `ev_calculator.py`, change the function signature and return values:

```python
def fetch_csfloat_price(skin_name, condition):
    """Fetch lowest CSFloat buy-now price for a skin+condition.

    Returns:
      (price, count) where:
        price > 0, count >= 1  — CSFloat has listing(s) at this price
        (CSFLOAT_NO_LISTING, 0) — CSFloat responded 200 OK but zero listings
        (0, 0)                  — Rate-limited/error; do NOT cache
    """
    if not _csfloat_has_budget():
        return 0, 0
    market_hash_name = f"{skin_name} ({condition})"
    params = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": 5,  # Was 1 — fetch a few to check for singletons
        "type": "buy_now",
    }
    max_attempts = 5
    for attempt in range(max_attempts):
        api_key, key_idx = _csfloat_multi_limiter.acquire()
        try:
            headers = {"Authorization": api_key}
            r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=10)
            _update_csfloat_budget(r)
            if r.status_code == 429:
                _csfloat_multi_limiter.report_429(key_idx)
                rl_remaining = _csfloat_budget["remaining"]
                rl_limit = _csfloat_budget["limit"]
                rl_reset = _csfloat_budget["reset"]
                wait_time = 5
                if rl_reset:
                    wait_time = max(1, rl_reset - int(time.time()) + 1)
                    if wait_time > 300:
                        print(f"   [CSFLOAT 429] attempt {attempt+1}/{max_attempts} key#{key_idx} | {rl_remaining}/{rl_limit} remaining, reset in {wait_time}s (>5m, giving up)")
                        return 0, 0
                else:
                    wait_time = min(5 * (2 ** attempt), 60)
                print(f"   [CSFLOAT 429] attempt {attempt+1}/{max_attempts} key#{key_idx} | {rl_remaining}/{rl_limit} remaining | waiting {wait_time}s")
                time.sleep(wait_time)
                continue
            if r.status_code in (401, 403):
                print(f"   [CSFLOAT] Auth error {r.status_code} key#{key_idx} for '{market_hash_name}' — check API key")
                return 0, 0
            if r.ok:
                listings = r.json().get("data", [])
                if listings:
                    return int(listings[0].get("price", 0)), len(listings)
                else:
                    return CSFLOAT_NO_LISTING, 0
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
            pass
    return 0, 0
```

- [ ] **Step 2: Update all callers of `fetch_csfloat_price` to unpack (price, count)**

There are 4 call sites (lines ~2065, ~2829, ~3322, and any in phase2_calculate_ev). Each currently does:

```python
return name_cond, fetch_csfloat_price(name, cond)
```

Change to unpack the tuple. Search for all `fetch_csfloat_price(` calls and update:

```python
# Line ~2065 (output prefetch)
return name_cond, fetch_csfloat_price(name, cond)
# Caller unpacks as: (name, cond), (price, count) = future.result()

# Line ~2829 (Pass 2 deep verify)
return name_cond, fetch_csfloat_price(name, cond)
# Same pattern

# Line ~3322 (phase2_multi_collection_ev — dead code but update for consistency)
return (name, cond), fetch_csfloat_price(name, cond)
```

At each call site where the result is consumed, change from:
```python
(name, cond), price = future.result()
```
to:
```python
(name, cond), (price, count) = future.result()
```

- [ ] **Step 3: Add sanity check in Pass 2 deep verify (lines ~2833-2848)**

After fetching CSFloat price, cross-reference against Steam median and Skinport. Reject if CSFloat price > 3x the reference AND count == 1 (singleton). If count >= 2 and still >3x, warn but accept (multiple sellers agree on price = real market).

Replace the price storage block in `phase2_pass2_deep_verify`:

```python
    csfloat_rejected_inflated = 0

    if to_verify:
        def _fetch_output_price(name_cond):
            name, cond = name_cond
            return name_cond, fetch_csfloat_price(name, cond)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(_fetch_output_price, pair): pair for pair in to_verify}
            for future in as_completed(futures):
                (name, cond), (price, count) = future.result()
                cache_key = f"{name}|{cond}"
                if price > 0:
                    # Sanity check: cross-reference against Steam/Skinport/DMarket
                    ref_price = None
                    ref_source = None

                    # Check Steam cache
                    for src_key, src_val in cached_prices.items():
                        if src_key == cache_key and price_sources.get(src_key) == "Steam" and src_val > 0:
                            ref_price = src_val
                            ref_source = "Steam"
                            break

                    # Check Skinport
                    if ref_price is None:
                        sp_data = get_skinport_price(skinport_prices, name, cond)
                        if sp_data and sp_data.get("price", 0) > 0 and sp_data.get("quantity", 0) >= 2:
                            ref_price = sp_data["price"]
                            ref_source = "Skinport"

                    # Check DMarket cache
                    if ref_price is None:
                        dmarket_items_raw, _, _ = load_dmarket_cache()
                        if dmarket_items_raw:
                            dm_title = f"{name} ({cond})"
                            for item in dmarket_items_raw:
                                if item.get("title", "") == dm_title:
                                    dm_price = item.get("price_usd", 0)
                                    if dm_price > 0:
                                        if ref_price is None or dm_price < ref_price:
                                            ref_price = dm_price
                                            ref_source = "DMarket"

                    # Apply sanity check
                    if ref_price and ref_price > 0 and price > ref_price * 3:
                        if count <= 1:
                            # Singleton at 3x+ reference = reject
                            print(f"   [REJECTED] {name} ({cond}): CSFloat ${price/100:.2f} (1 listing) vs {ref_source} ${ref_price/100:.2f} ({price/ref_price:.1f}x)")
                            csfloat_rejected_inflated += 1
                            # Don't cache — let it be retried next run
                            continue
                        else:
                            # Multiple listings at 3x+ — warn but accept (market might be real)
                            print(f"   [WARN] {name} ({cond}): CSFloat ${price/100:.2f} ({count} listings) vs {ref_source} ${ref_price/100:.2f} ({price/ref_price:.1f}x)")

                    cached_prices[cache_key] = price
                    price_sources[cache_key] = "CSFloat"
                    cache_timestamps[cache_key] = time.time()
                    csfloat_ok += 1
                elif price == CSFLOAT_NO_LISTING:
                    if cache_key not in cached_prices or cached_prices[cache_key] <= 0:
                        cached_prices[cache_key] = CSFLOAT_NO_LISTING
                        cache_timestamps[cache_key] = time.time()
                    csfloat_no_listing += 1
                else:
                    csfloat_failed += 1

        print(f"   CSFloat: {csfloat_ok} priced, {csfloat_no_listing} no listing, {csfloat_failed} failed")
        if csfloat_rejected_inflated:
            print(f"   CSFloat rejected: {csfloat_rejected_inflated} inflated singletons (>3x reference)")
```

**Important:** The DMarket cache lookup should be done ONCE before the loop (load it into a dict), not per-item. Refactor:

```python
    # Pre-load DMarket refs for sanity check (same pattern as Skinport sanity check)
    dmarket_refs_for_sanity = {}
    dmarket_items_raw, _, _ = load_dmarket_cache()
    if dmarket_items_raw:
        for item in dmarket_items_raw:
            title = item.get("title", "")
            dm_price = item.get("price_usd", 0)
            if title and dm_price > 0:
                if title not in dmarket_refs_for_sanity or dm_price < dmarket_refs_for_sanity[title]:
                    dmarket_refs_for_sanity[title] = dm_price
```

Then inside the loop, replace the DMarket check with:
```python
    if ref_price is None:
        dm_title = f"{name} ({cond})"
        dm_price = dmarket_refs_for_sanity.get(dm_title)
        if dm_price and dm_price > 0:
            ref_price = dm_price
            ref_source = "DMarket"
```

- [ ] **Step 4: Run the script and verify the P90 Baroque Red is rejected**

First clear the stale cache entry:
```bash
cd "C:\Users\oskar\Desktop\Claude-code"
python -c "
import json
d = json.load(open('price_cache.json'))
entries = d.get('entries', {})
key = 'P90 | Baroque Red|Field-Tested'
if key in entries:
    del entries[key]
    print(f'Deleted {key}')
    json.dump(d, open('price_cache.json', 'w'))
else:
    print('Key not found')
"
```

Then run:
```bash
python ev_calculator.py
```

Expected: P90 Baroque Red should either (a) be rejected with `[REJECTED]` message if CSFloat still has a singleton at $809, or (b) get a sane price. The Canals trade-up should no longer show +207% ROI.

- [ ] **Step 5: Commit**

```bash
git add ev_calculator.py
git commit -m "fix: add CSFloat output price sanity check (3x ref, singleton filter)

CSFloat prices were accepted without validation. A single inflated
listing (e.g. P90 Baroque Red at $809 vs real ~$5) produced fake
profitable trade-ups. Now cross-references against Steam/Skinport/
DMarket and rejects singleton listings priced >3x the reference."
```

---

### Task 2: Fix multi-collection trade-ups producing 0 candidates

**Why this matters:** The multi-collection code at line 2664-2666 skips any target collection where `ev_per_input <= 0`. This happens when target outputs have no cached price AND no Skinport price. On a fresh cache or for low-volume collections (which are exactly the ones that benefit most from fillers), this kills all multi-collection candidates.

**Files:**
- Modify: `ev_calculator.py:2597-2621` (ev_per_input pre-computation)
- Modify: `ev_calculator.py:2659-2666` (target collection filtering)

- [ ] **Step 1: Fix ev_per_input to use Steam on-the-fly for unpriced outputs**

The issue is `ev_per_input` only checks `cached_prices` (line 2610). If a target collection's outputs have no cached price, `ev_per_input = 0` and the collection is skipped.

But Pass 1 is supposed to use "free data only" — making Steam API calls defeats that. Instead, the fix should be: **don't skip collections with `ev_per_input = 0` — just deprioritize them.** The fillers might still make the trade-up worth verifying in Pass 2.

Replace lines ~2664-2666:

```python
        for target_coll in colls_with_outputs.get(out_rarity, set()):
            target_normal = coll_inputs.get(target_coll, [])
            if len(target_normal) < 1:
                continue

            target_ev_per_slot = ev_per_input.get((target_coll, out_rarity), 0)
            # Don't skip ev=0 targets — they may have unpriced valuable outputs
            # that Pass 2 will verify. Just skip if we KNOW outputs are worthless
            # (all outputs confirmed NO_LISTINGS).
            all_no_listing = True
            target_outputs = coll_skins.get(target_coll, {}).get(out_rarity, [])
            for out in target_outputs:
                for try_cond in ["Factory New", "Minimal Wear", "Field-Tested"]:
                    cache_key = f"{out['name']}|{try_cond}"
                    cached_val = cached_prices.get(cache_key, 0)
                    if cached_val != CSFLOAT_NO_LISTING:
                        all_no_listing = False
                        break
                if not all_no_listing:
                    break
            if all_no_listing and len(target_outputs) > 0:
                continue  # All outputs confirmed dead — no point
```

- [ ] **Step 2: Mark unpriced multi-collection candidates for Pass 2 verification**

When `ev_per_input = 0` for a target, the `_evaluate_tradeup` call may return `None` (no prices → `has_price = False`). The fix is to ensure unpriced outputs get added to `outputs_to_verify` so Pass 2 fetches their CSFloat prices.

After the `_evaluate_tradeup` call at line ~2733, add fallback logic:

```python
                result = _evaluate_tradeup(
                    all_10_items, coll_skins, out_rarity, cached_prices, cached_sources,
                    skinport_prices, cache_timestamps, cache_volumes,
                    apply_liquidity=False, relaxed_filters=True,
                    coll_counts=dict(coll_counts)
                )

                # Even if result is None (no prices), still mark outputs for verification
                # if this looks like a potentially cheap trade-up
                if result is None:
                    total_input_cost = sum(x.get("_best_price", 0) for x in all_10_items)
                    if total_input_cost > 0 and total_input_cost < 5000:  # <$50 input cost
                        for coll in coll_counts:
                            for out in coll_skins.get(coll, {}).get(out_rarity, []):
                                for try_cond in ["Factory New", "Minimal Wear", "Field-Tested"]:
                                    ck = f"{out['name']}|{try_cond}"
                                    if cached_prices.get(ck, 0) <= 0:
                                        outputs_to_verify.add((out["name"], try_cond))
                    continue
```

- [ ] **Step 3: Run and verify multi-collection candidates > 0**

```bash
python ev_calculator.py 2>&1 | grep -i "multi-collection"
```

Expected: `Multi-collection candidates: N` where N > 0.

- [ ] **Step 4: Commit**

```bash
git add ev_calculator.py
git commit -m "fix: multi-collection trade-ups now produce candidates

Previously skipped any target collection with ev_per_input=0, which
happened for all unpriced outputs. Now only skips collections where
ALL outputs are confirmed NO_LISTINGS. Unpriced outputs from cheap
trade-ups are marked for CSFloat verification in Pass 2."
```

---

### Task 3: Add "low adjusted float" filler strategy

**Why this matters:** This is the user's key insight. The current filler strategies are: (A) cheapest, (B) best EV/dollar, (C) max jackpot. None optimize for what actually makes fillers valuable in practice: **lowering the average adjusted float**.

Lower avg adjusted float → better output condition (FT→MW, MW→FN) → dramatically higher output price. A filler at $0.50 with adjusted float 0.05 is worth far more than a filler at $0.10 with adjusted float 0.70, because it pulls the average down and may push outputs from FT ($2) to MW ($20).

**Files:**
- Modify: `ev_calculator.py:2682-2711` (Pass 1 filler selection — add strategy D)
- Modify: `ev_calculator.py:3510-3549` (Pass 2b / phase2_multi_collection_ev — add strategy D, if this code is ever called)

- [ ] **Step 1: Add adjusted float to filler selection data in Pass 1 broad scan**

Currently filler items have `extra.skin_min`, `extra.skin_max`, `extra.floatValue`. We need the adjusted float for scoring. Add it inline where fillers are built (line ~2683):

```python
                # Cheapest filler strategy + low-float strategy for Pass 1
                filler_by_coll = defaultdict(list)
                for inp, inp_coll in pool:
                    if inp_coll == target_coll:
                        continue
                    lid = inp.get("_listing_id", "")
                    if lid in target_listing_ids:
                        continue
                    filler_max_adj = coll_max_adjusted.get((inp_coll, out_rarity))
                    if filler_max_adj is not None:
                        extra = inp.get("extra", {})
                        skin_min = extra.get("skin_min", 0)
                        skin_max = extra.get("skin_max", 1)
                        max_raw = calc_max_input_float_for_skin(filler_max_adj, skin_min, skin_max)
                        raw_float = extra.get("floatValue", 0)
                        if max_raw is not None and raw_float > max_raw + 0.0001:
                            continue
                    else:
                        extra = inp.get("extra", {})
                        skin_min = extra.get("skin_min", 0)
                        skin_max = extra.get("skin_max", 1)
                        raw_float = extra.get("floatValue", 0)

                    # Pre-compute adjusted float for this filler
                    skin_range = skin_max - skin_min
                    if skin_range > 0:
                        adj_float = (raw_float - skin_min) / skin_range
                    else:
                        adj_float = 0.0
                    inp["_adj_float"] = adj_float

                    filler_by_coll[inp_coll].append(inp)
```

- [ ] **Step 2: Add Strategy D — lowest adjusted float fillers**

After the cheapest filler strategy (Strategy A) at line ~2709, add Strategy D:

```python
                # Strategy A: cheapest average filler cost
                def _avg_cost(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    return -(sum(f.get("_best_price", 999999) for f in f_inputs[:n]) / n) if n > 0 else 0
                fillers_a = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _avg_cost)
                if len(fillers_a) >= needed:
                    all_10_items = list(target_selected) + [f[0] for f in fillers_a]
                    # ... existing evaluation code ...

                # Strategy D: lowest adjusted float fillers (pushes output condition up)
                # Sort each collection's fillers by adjusted float ascending
                filler_by_coll_sorted_float = {}
                for f_coll, f_inputs in filler_by_coll.items():
                    filler_by_coll_sorted_float[f_coll] = sorted(
                        f_inputs, key=lambda x: x.get("_adj_float", 1.0)
                    )

                def _lowest_adj_float(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    # Score: negative mean adjusted float (lower = better = higher score)
                    avg_adj = sum(f.get("_adj_float", 1.0) for f in f_inputs[:n]) / n if n > 0 else 1.0
                    return -avg_adj  # _select_fillers sorts reverse=True, so negative = prefer lower

                fillers_d = _select_fillers(filler_by_coll_sorted_float, needed, target_listing_ids, MAX_COLLECTIONS - 1, _lowest_adj_float)
                if len(fillers_d) >= needed:
                    ids_d = {f[0].get("_listing_id", "") for f in fillers_d}
                    ids_a = {f[0].get("_listing_id", "") for f in fillers_a} if len(fillers_a) >= needed else set()
                    if ids_d != ids_a:  # Don't duplicate if same set
                        all_10_items_d = list(target_selected) + [f[0] for f in fillers_d]
                        all_10_colls_d = [(inp, target_coll) for inp in target_selected] + fillers_d
                        # ... evaluate this combination same as Strategy A ...
```

- [ ] **Step 3: Restructure the evaluation loop to handle multiple strategies**

The current code evaluates Strategy A inline. Restructure to evaluate all strategies in a loop. Replace the section from line ~2704 to ~2756 with:

```python
                # Collect candidate filler sets from multiple strategies
                candidate_fills = []

                # Strategy A: cheapest average filler cost
                def _avg_cost(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    return -(sum(f.get("_best_price", 999999) for f in f_inputs[:n]) / n) if n > 0 else 0
                fillers_a = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _avg_cost)
                if len(fillers_a) >= needed:
                    candidate_fills.append(fillers_a)

                # Strategy D: lowest adjusted float (push output condition up)
                filler_by_coll_low_float = {}
                for f_coll, f_inputs in filler_by_coll.items():
                    filler_by_coll_low_float[f_coll] = sorted(
                        f_inputs, key=lambda x: x.get("_adj_float", 1.0)
                    )
                def _lowest_adj_float(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    avg_adj = sum(f.get("_adj_float", 1.0) for f in f_inputs[:n]) / n if n > 0 else 1.0
                    return -avg_adj
                fillers_d = _select_fillers(filler_by_coll_low_float, needed, target_listing_ids, MAX_COLLECTIONS - 1, _lowest_adj_float)
                if len(fillers_d) >= needed:
                    ids_d = {f[0].get("_listing_id", "") for f in fillers_d}
                    ids_a = {f[0].get("_listing_id", "") for f in fillers_a} if len(fillers_a) >= needed else set()
                    if ids_d != ids_a:
                        candidate_fills.append(fillers_d)

                if not candidate_fills:
                    continue

                for fillers in candidate_fills:
                    all_10_items = list(target_selected) + [f[0] for f in fillers]
                    all_10_colls = [(inp, target_coll) for inp in target_selected] + fillers

                    listing_ids = sorted(x.get("_listing_id", "") for x in all_10_items)
                    non_empty = [lid for lid in listing_ids if lid]
                    if len(non_empty) < 10:
                        dedup_key = tuple(sorted(
                            f"{x.get('_listing_id', '')}_{x.get('_best_price', 0)}"
                            for x in all_10_items
                        ))
                    else:
                        dedup_key = tuple(non_empty)
                    if dedup_key in seen_tradeups:
                        continue
                    seen_tradeups.add(dedup_key)

                    coll_counts = defaultdict(int)
                    for inp, coll in all_10_colls:
                        coll_counts[coll] += 1

                    result = _evaluate_tradeup(
                        all_10_items, coll_skins, out_rarity, cached_prices, cached_sources,
                        skinport_prices, cache_timestamps, cache_volumes,
                        apply_liquidity=False, relaxed_filters=True,
                        coll_counts=dict(coll_counts)
                    )
                    if result is None:
                        # Mark unpriced outputs for verification if cheap trade-up
                        total_input_cost = sum(x.get("_best_price", 0) for x in all_10_items)
                        if total_input_cost > 0 and total_input_cost < 5000:
                            for coll in coll_counts:
                                for out in coll_skins.get(coll, {}).get(out_rarity, []):
                                    for try_cond in ["Factory New", "Minimal Wear", "Field-Tested"]:
                                        ck = f"{out['name']}|{try_cond}"
                                        if cached_prices.get(ck, 0) <= 0:
                                            outputs_to_verify.add((out["name"], try_cond))
                        continue

                    multi_count += 1
                    filler_names = sorted(c for c in coll_counts if c != target_coll)
                    coll_label = f"{target_coll} + {' + '.join(filler_names)}"
                    mix_desc = ", ".join(f"{c}({n})" for c, n in sorted(coll_counts.items(), key=lambda x: -x[1]))

                    result["collection"] = coll_label
                    result["target_collection"] = target_coll
                    result["coll_mix"] = mix_desc
                    result["in_rarity"] = in_rarity
                    result["out_rarity"] = out_rarity
                    result["is_stattrak"] = False
                    result["multi_collection"] = True
                    result["inputs_raw"] = all_10_items
                    result["inputs_raw_with_colls"] = all_10_colls
                    candidates.append(result)

                    for o in result["outputs"]:
                        if o["price_raw"] > 0 and o["confidence"] != "high":
                            outputs_to_verify.add((o["name"], o["condition"]))
                        elif o["price_raw"] <= 0 and o["price_source"] != "NO_LISTINGS":
                            outputs_to_verify.add((o["name"], o["condition"]))
```

- [ ] **Step 4: Also add Strategy D to `phase2_multi_collection_ev` (the 3-strategy function)**

This function at line ~3510 already has strategies A, B, C. Add D after C:

```python
                # Strategy D: lowest adjusted float
                filler_by_coll_low_float = {}
                for f_coll, f_inputs in filler_by_coll.items():
                    filler_by_coll_low_float[f_coll] = sorted(
                        f_inputs, key=lambda x: x.get("_adj_float", 1.0)
                    )
                def _lowest_adj_float(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    avg_adj = sum(f.get("_adj_float", 1.0) for f in f_inputs[:n]) / n if n > 0 else 1.0
                    return -avg_adj
                fillers_d = _select_fillers(filler_by_coll_low_float, needed, target_listing_ids, MAX_COLLECTIONS - 1, _lowest_adj_float)
                if len(fillers_d) >= needed:
                    ids_d = {f[0].get("_listing_id", "") for f in fillers_d}
                    # Check not duplicate of A, B, or C
                    existing_ids = [
                        {f[0].get("_listing_id", "") for f in fills}
                        for fills in candidate_fills
                    ]
                    if ids_d not in existing_ids:
                        candidate_fills.append(fillers_d)
```

Also ensure `_adj_float` is computed for fillers in phase2_multi_collection_ev. Find where fillers are built (line ~3483) and add the same adjusted float computation as in Step 1.

- [ ] **Step 5: Run and verify low-float fillers produce different (better) results**

```bash
python ev_calculator.py 2>&1 | grep -E "multi-collection|Multi-collection"
```

Expected: More multi-collection candidates than before. Some should show lower avg adjusted floats → better output conditions → higher EV.

- [ ] **Step 6: Commit**

```bash
git add ev_calculator.py
git commit -m "feat: add low-adjusted-float filler strategy for multi-collection trade-ups

New Strategy D picks fillers with the lowest adjusted floats instead of
cheapest price. Lower avg adjusted float pushes output conditions higher
(e.g. FT to MW), dramatically increasing output value. The extra cost
of slightly pricier fillers is often dwarfed by the condition upgrade."
```

---

### Task 4: Integration test — full run with all fixes

**Files:**
- No new files — run the existing script

- [ ] **Step 1: Clear stale cache entries that may have inflated prices**

```bash
cd "C:\Users\oskar\Desktop\Claude-code"
python -c "
import json
d = json.load(open('price_cache.json'))
entries = d.get('entries', {})
# Remove known inflated entry
for key in list(entries.keys()):
    if 'Baroque Red' in key:
        del entries[key]
        print(f'Removed: {key}')
json.dump(d, open('price_cache.json', 'w'))
"
```

- [ ] **Step 2: Run full script**

```bash
python ev_calculator.py
```

- [ ] **Step 3: Verify all three fixes working**

Check output for:
1. `CSFloat rejected: N inflated singletons` — sanity check working
2. `Multi-collection candidates: N` where N > 0 — filler system working
3. Look at multi-collection results — some should show MW/FN outputs from the low-float filler strategy
4. No fake $800+ output prices in profitable results

- [ ] **Step 4: Commit any final adjustments**

```bash
git add ev_calculator.py
git commit -m "test: verified all three fixes working in full run"
```
