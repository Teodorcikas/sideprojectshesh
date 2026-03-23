"""Test CSFloat API params to maximize data per request."""
import requests
import time
import json

API_KEY = "skYpZbif0-zYaiAA1nxlQmwL1AsGAZrN"
URL = "https://csfloat.com/api/v1/listings"
HEADERS = {"Authorization": API_KEY}

def test_param(name, params, expect_key="data"):
    """Make a request and report results."""
    try:
        r = requests.get(URL, params=params, headers=HEADERS, timeout=15)
        remaining = r.headers.get("X-RateLimit-Remaining", "?")
        limit = r.headers.get("X-RateLimit-Limit", "?")
        reset = r.headers.get("X-RateLimit-Reset", "?")

        if r.status_code == 429:
            reset_in = int(reset) - int(time.time()) if reset != "?" else "?"
            print(f"  RATE LIMITED — {remaining}/{limit} remaining, reset in {reset_in}s")
            return None

        print(f"  [{remaining}/{limit} remaining]", end=" ")

        if not r.ok:
            print(f"FAIL — HTTP {r.status_code}: {r.text[:200]}")
            return None

        data = r.json()
        listings = data.get(expect_key, [])
        print(f"OK — {len(listings)} listings returned")

        # Show a sample
        if listings and len(listings) > 0:
            sample = listings[0]
            item = sample.get("item", {})
            print(f"    Sample: {item.get('market_hash_name', '?')} | "
                  f"float={item.get('float_value', '?')} | "
                  f"rarity={item.get('rarity_name', '?')} | "
                  f"price={sample.get('price', '?')} cents | "
                  f"souvenir={item.get('is_souvenir', '?')}")
        return listings
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


# Check rate limit first
print("Checking rate limit status...")
r = requests.get(URL, params={"limit": 1, "type": "buy_now"}, headers=HEADERS, timeout=15)
remaining = r.headers.get("X-RateLimit-Remaining", "?")
limit = r.headers.get("X-RateLimit-Limit", "?")
reset_ts = r.headers.get("X-RateLimit-Reset", "?")

if r.status_code == 429:
    try:
        wait = int(reset_ts) - int(time.time())
        print(f"Rate limited! {remaining}/{limit}, resets in {wait}s ({wait/60:.1f}m)")
        if wait > 0:
            print(f"Waiting {wait}s for reset...")
            time.sleep(wait + 2)
            print("Reset! Continuing...\n")
    except:
        print("Rate limited, can't parse reset time. Exiting.")
        exit(1)
else:
    print(f"Budget: {remaining}/{limit} remaining\n")

# ========== TEST 1: Higher limit ==========
print("=" * 60)
print("TEST 1: limit=500 (can we get more than 50 per request?)")
print("=" * 60)
result = test_param("limit=500", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 500,
})
if result is not None:
    print(f"  → limit=500 returns {len(result)} items")
    if len(result) > 50:
        print(f"  ★ YES! We can get {len(result)} per request instead of 50!")
    elif len(result) == 50:
        print(f"  ✗ Capped at 50 — server ignores limit > 50")

time.sleep(1)

# ========== TEST 2: limit=100 ==========
print("\n" + "=" * 60)
print("TEST 2: limit=100")
print("=" * 60)
result = test_param("limit=100", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 100,
})
if result is not None:
    print(f"  → limit=100 returns {len(result)} items")

time.sleep(1)

# ========== TEST 3: max_float filter ==========
print("\n" + "=" * 60)
print("TEST 3: max_float=0.38 (exclude WW and BS server-side)")
print("=" * 60)
result = test_param("max_float=0.38", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 50, "max_float": 0.38,
})
if result is not None:
    floats = [l.get("item", {}).get("float_value", 0) for l in result]
    max_fv = max(floats) if floats else 0
    print(f"  → Max float in results: {max_fv:.4f} (should be < 0.38)")

time.sleep(1)

# ========== TEST 4: max_float=0.15 (MW and FN only) ==========
print("\n" + "=" * 60)
print("TEST 4: max_float=0.15 (only FN/MW — the low-float skins we want)")
print("=" * 60)
result = test_param("max_float=0.15", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 50, "max_float": 0.15,
})
if result is not None:
    floats = [l.get("item", {}).get("float_value", 0) for l in result]
    max_fv = max(floats) if floats else 0
    print(f"  → Max float in results: {max_fv:.4f} (should be < 0.15)")

time.sleep(1)

# ========== TEST 5: is_souvenir param ==========
print("\n" + "=" * 60)
print("TEST 5: is_souvenir=false (exclude souvenirs server-side)")
print("=" * 60)
result = test_param("is_souvenir=false", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 50, "is_souvenir": "false",
})

time.sleep(1)

# ========== TEST 6: rarity filter ==========
print("\n" + "=" * 60)
print("TEST 6: rarity=consumer_grade (test if rarity param works)")
print("=" * 60)
# Try different rarity param formats
for rarity_val in ["consumer_grade", "Consumer Grade", "1", "consumer grade"]:
    print(f"\n  Trying rarity={rarity_val}...")
    result = test_param(f"rarity={rarity_val}", {
        "sort_by": "lowest_price", "type": "buy_now", "category": 1,
        "limit": 5, "rarity": rarity_val,
    })
    if result is not None and len(result) > 0:
        rarities = set(l.get("item", {}).get("rarity_name", "?") for l in result)
        print(f"    Rarities returned: {rarities}")
    time.sleep(0.5)

# ========== TEST 7: Combined optimal params ==========
print("\n" + "=" * 60)
print("TEST 7: Combined — limit=500 + max_float=0.38 + category=1")
print("=" * 60)
result = test_param("combined", {
    "sort_by": "lowest_price", "type": "buy_now", "category": 1,
    "limit": 500, "max_float": 0.38,
})
if result is not None:
    floats = [l.get("item", {}).get("float_value", 0) for l in result]
    souvenirs = sum(1 for l in result if "Souvenir" in l.get("item", {}).get("market_hash_name", ""))
    print(f"  → {len(result)} items, max float {max(floats):.4f}, {souvenirs} souvenirs")

# ========== SUMMARY ==========
print("\n" + "=" * 60)
print("BUDGET REMAINING:")
print("=" * 60)
r2 = requests.get(URL, params={"limit": 1, "type": "buy_now"}, headers=HEADERS, timeout=15)
print(f"  {r2.headers.get('X-RateLimit-Remaining', '?')}/{r2.headers.get('X-RateLimit-Limit', '?')} remaining")
