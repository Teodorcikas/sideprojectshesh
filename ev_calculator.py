import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import sys
import json
import os
import time

sys.stdout.reconfigure(encoding='utf-8')

DMARKET_URL = "https://api.dmarket.com/exchange/v1/market/items"
SKINPORT_URL = "https://api.skinport.com/v1/items"
CSFLOAT_URL = "https://csfloat.com/api/v1/listings"
CSFLOAT_API_KEY = "skYpZbif0-zYaiAA1nxlQmwL1AsGAZrN"
STEAM_URL = "https://steamcommunity.com/market/priceoverview/"
SKINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "price_cache.json")
SKINPORT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "skinport_cache.json")
DMARKET_CACHE_FILE = os.path.join(os.path.dirname(__file__), "dmarket_cache.json")
CSFLOAT_INPUT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "csfloat_input_cache.json")
SKINPORT_RATELIMIT_FILE = os.path.join(os.path.dirname(__file__), "skinport_lastcall.txt")
CACHE_EXPIRY = 3 * 60 * 60  # 3 hours
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours (keep stale data as fallback)
SKINPORT_COOLDOWN = 60  # 1 minute between Skinport API calls
CSFLOAT_INPUT_RATE_LIMIT = 0.5  # 500ms between CSFloat listing requests

STEAM_SELLER_FEE = 0.15
DMARKET_BUYER_FEE = 0.0  # No buyer fee on DMarket
SKINPORT_BUYER_FEE = 0.0  # No buyer fee on Skinport
CSFLOAT_SELLER_FEE = 0.02  # 2% seller fee on CSFloat (where we'll sell outputs)
# For now, estimate output value using Skinport/DMarket prices, apply CSFloat fee
TOTAL_SELL_FEE = CSFLOAT_SELLER_FEE  # 2% when selling on CSFloat

RARITY_ORDER = {
    "consumer grade": 0, "industrial grade": 1, "mil-spec grade": 2,
    "restricted": 3, "classified": 4, "covert": 5,
}
RARITY_NAMES = list(RARITY_ORDER.keys())

FLOAT_RANGES = [
    ("Factory New", 0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn", 0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]

VALID_CATEGORIES = {"Rifles", "SMGs", "Pistols", "Heavy", "Equipment"}


def get_condition(fv):
    for name, lo, hi in FLOAT_RANGES:
        if lo <= fv < hi:
            return name
    return "Battle-Scarred"


def calc_output_float(inputs, out_min, out_max):
    """Calculate output float using October 2025 algorithm.

    inputs: list of dicts with 'float', 'skin_min', 'skin_max' keys
    For each input: adjusted = (float - skin_min) / (skin_max - skin_min)
    Output = out_min + avg_adjusted * (out_max - out_min)
    """
    adjusted_floats = []
    for inp in inputs:
        skin_float = inp['float']
        skin_min = inp['skin_min']
        skin_max = inp['skin_max']
        skin_range = skin_max - skin_min
        if skin_range > 0:
            adjusted = (skin_float - skin_min) / skin_range
        else:
            adjusted = 0
        adjusted_floats.append(adjusted)

    avg_adjusted = sum(adjusted_floats) / len(adjusted_floats)
    return out_min + avg_adjusted * (out_max - out_min)


def calc_max_adjusted_float(out_min, out_max, target_output=0.15):
    """Calculate max adjusted input float to achieve target output float (MW threshold).

    Returns max allowed average adjusted float for MW output.
    """
    if out_max <= out_min:
        return None
    max_adjusted = (target_output - out_min) / (out_max - out_min)
    return max_adjusted if max_adjusted > 0 else None


def calc_max_input_float_for_skin(max_adjusted, skin_min, skin_max):
    """Convert max adjusted float to max raw float for a specific input skin.

    max_raw = skin_min + max_adjusted * (skin_max - skin_min)
    """
    if max_adjusted is None:
        return None
    max_raw = skin_min + max_adjusted * (skin_max - skin_min)
    # Must be >= FT minimum (0.15) to be useful
    return max_raw if max_raw >= 0.15 else None


def build_input_float_limits(coll_skins):
    """Build max adjusted float limits per collection+rarity based on output skins.

    Returns max_adjusted values (not raw floats) - must be converted per-skin during filtering.
    """
    limits = {}  # (collection, input_rarity) -> max_adjusted_float

    for coll_name, rarities in coll_skins.items():
        for out_rarity, skins in rarities.items():
            in_rarity = out_rarity - 1  # Input is one rarity below output
            if in_rarity < 0:
                continue

            # Find most restrictive max_adjusted_float across all outputs
            min_limit = None
            for skin in skins:
                out_min = skin.get("min_float", 0.0)
                out_max = skin.get("max_float", 1.0)
                limit = calc_max_adjusted_float(out_min, out_max)
                if limit is not None:
                    if min_limit is None or limit < min_limit:
                        min_limit = limit

            if min_limit is not None:
                limits[(coll_name, in_rarity)] = min_limit

    return limits


# ============ CACHING ============

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}, 0
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("prices", {}), data.get("timestamp", 0)
    except:
        return {}, 0


def save_cache(prices):
    data = {"timestamp": time.time(), "prices": prices}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def is_cache_valid(timestamp):
    return (time.time() - timestamp) < CACHE_EXPIRY


def load_skinport_cache():
    """Load Skinport cache with stale fallback support."""
    if not os.path.exists(SKINPORT_CACHE_FILE):
        return {}, 0, "none"
    try:
        with open(SKINPORT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < CACHE_EXPIRY:
            return data.get("prices", {}), timestamp, "fresh"
        elif age < CACHE_STALE_EXPIRY:
            return data.get("prices", {}), timestamp, "stale"
        else:
            # Too old, delete it
            os.remove(SKINPORT_CACHE_FILE)
            return {}, 0, "expired"
    except:
        return {}, 0, "error"


def save_skinport_cache(prices):
    """Save Skinport prices to cache."""
    data = {"timestamp": time.time(), "prices": prices}
    with open(SKINPORT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_dmarket_cache():
    """Load DMarket cache with stale fallback support."""
    if not os.path.exists(DMARKET_CACHE_FILE):
        return [], 0, "none"
    try:
        with open(DMARKET_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < CACHE_EXPIRY:
            return data.get("items", []), timestamp, "fresh"
        elif age < CACHE_STALE_EXPIRY:
            return data.get("items", []), timestamp, "stale"
        else:
            # Too old, delete it
            os.remove(DMARKET_CACHE_FILE)
            return [], 0, "expired"
    except:
        return [], 0, "error"


def save_dmarket_cache(items):
    """Save DMarket listings to cache."""
    data = {"timestamp": time.time(), "items": items}
    with open(DMARKET_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_csfloat_input_cache():
    """Load CSFloat input listings cache with stale fallback support."""
    if not os.path.exists(CSFLOAT_INPUT_CACHE_FILE):
        return [], 0, "none"
    try:
        with open(CSFLOAT_INPUT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < CACHE_EXPIRY:
            return data.get("items", []), timestamp, "fresh"
        elif age < CACHE_STALE_EXPIRY:
            return data.get("items", []), timestamp, "stale"
        else:
            os.remove(CSFLOAT_INPUT_CACHE_FILE)
            return [], 0, "expired"
    except:
        return [], 0, "error"


def save_csfloat_input_cache(items):
    """Save CSFloat input listings to cache."""
    data = {"timestamp": time.time(), "items": items}
    with open(CSFLOAT_INPUT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_skinport_last_call():
    """Get timestamp of last Skinport API call."""
    if not os.path.exists(SKINPORT_RATELIMIT_FILE):
        return 0
    try:
        with open(SKINPORT_RATELIMIT_FILE, "r") as f:
            return float(f.read().strip())
    except:
        return 0


def set_skinport_last_call():
    """Record current time as last Skinport API call."""
    with open(SKINPORT_RATELIMIT_FILE, "w") as f:
        f.write(str(time.time()))


def wait_for_skinport_cooldown():
    """Wait if needed to respect Skinport rate limit (1 min between calls)."""
    last_call = get_skinport_last_call()
    elapsed = time.time() - last_call
    if elapsed < SKINPORT_COOLDOWN:
        wait_time = SKINPORT_COOLDOWN - elapsed
        print(f"   [SKINPORT RATE LIMIT] Waiting {wait_time:.0f}s before API call...")
        time.sleep(wait_time)


# ============ PHASE 1: FETCH INPUTS ============

WEAPON_NAMES = [
    "AK-47", "M4A4", "M4A1-S", "AWP", "Desert Eagle", "USP-S", "Glock-18",
    "P250", "Five-SeveN", "Tec-9", "CZ75-Auto", "Dual Berettas", "R8 Revolver",
    "P2000", "MP9", "MAC-10", "MP7", "UMP-45", "P90", "PP-Bizon", "MP5-SD",
    "FAMAS", "Galil AR", "SG 553", "AUG", "SSG 08", "SCAR-20", "G3SG1",
    "Nova", "XM1014", "MAG-7", "Sawed-Off", "M249", "Negev"
]


def fetch_csfloat_ft_listings(max_items=2000):
    """Fetch FT listings from CSFloat with low floats (0.15-0.25 range)."""
    all_items = []
    headers = {"Authorization": CSFLOAT_API_KEY}

    # Fetch FT skins sorted by float (lowest first)
    page = 0
    while len(all_items) < max_items:
        params = {
            "min_float": 0.15,
            "max_float": 0.25,  # Focus on low-float FT for MW trade-ups
            "sort_by": "lowest_float",
            "type": "buy_now",
            "limit": 50,
            "page": page,
        }

        try:
            r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=15)
            if not r.ok:
                print(f"   [CSFLOAT] API error: {r.status_code}")
                break
            data = r.json()
            listings = data.get("data", [])
            if not listings:
                break

            for listing in listings:
                item = listing.get("item", {})
                fv = item.get("float_value")
                market_hash_name = item.get("market_hash_name", "")

                # Skip souvenir and StatTrak
                if "Souvenir" in market_hash_name:
                    continue

                # Must be Field-Tested
                if "(Field-Tested)" not in market_hash_name:
                    continue

                if fv is None or fv < 0.15 or fv > 0.38:
                    continue

                # Extract collection from item data
                collection = item.get("collection")
                if not collection:
                    continue
                coll_name = collection.lower().replace("the ", "").replace(" collection", "").strip()

                # Get rarity
                rarity = item.get("rarity_name", "").lower()
                if rarity not in RARITY_ORDER:
                    continue

                # Price is in cents
                price = listing.get("price", 0)

                all_items.append({
                    "title": market_hash_name,
                    "price_usd": price,
                    "float": fv,
                    "collection": coll_name,
                    "quality": rarity,
                    "source": "CSFloat",
                })

            page += 1
            time.sleep(CSFLOAT_INPUT_RATE_LIMIT)

            # CSFloat API usually has limited pages
            if page >= 40:  # Max ~2000 items
                break

        except Exception as e:
            print(f"   [CSFLOAT] Error: {e}")
            break

    return all_items


def fetch_weapon_raw(weapon_name, max_items=500):
    """Fetch raw FT items for a weapon from DMarket (for caching)."""
    all_items = []
    cursor = None

    while len(all_items) < max_items:
        params = {
            "gameId": "a8db",
            "title": weapon_name,
            "limit": 100,
            "orderBy": "price",
            "orderDir": "asc",
            "currency": "USD",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            r = requests.get(DMARKET_URL, params=params, timeout=15)
            if not r.ok:
                break
            data = r.json()
            batch = data.get("objects", [])
            if not batch:
                break

            for item in batch:
                extra = item.get("extra", {})
                fv = extra.get("floatValue")
                colls = extra.get("collection", [])
                quality = extra.get("quality", "").lower()
                title = item.get("title", "")

                # Skip souvenir
                if "Souvenir" in title:
                    continue

                if fv is None or not colls or quality not in RARITY_ORDER:
                    continue

                # Must be Field-Tested with float in possible MW range (0.15-0.38)
                if "(Field-Tested)" not in title:
                    continue

                # Store basic FT items for caching (filter by float_limits later)
                if 0.15 <= fv <= 0.38:
                    all_items.append({
                        "title": title,
                        "price_usd": int(item.get("price", {}).get("USD", 0)),
                        "float": fv,
                        "collection": colls[0].lower().replace("the ", "").replace(" collection", "").strip(),
                        "quality": quality,
                    })

            cursor = data.get("cursor")
            if not cursor:
                break
        except:
            break

    return all_items


def extract_skin_name(title):
    """Extract base skin name from title (e.g., 'AK-47 | Redline (Field-Tested)' -> 'AK-47 | Redline')."""
    # Remove condition suffix
    for cond in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
        title = title.replace(cond, "")
    # Remove StatTrak prefix for lookup
    title = title.replace("StatTrak™ ", "").replace("StatTrak ", "")
    return title.strip()


def process_cached_items(raw_items, float_limits, skinport_prices, skin_float_ranges):
    """Process cached DMarket/CSFloat items with current float_limits and Skinport prices.

    Uses October 2025 algorithm: per-skin max float based on skin's own float range.
    """
    processed = []
    for item in raw_items:
        coll = item["collection"]
        in_rarity = RARITY_ORDER.get(item["quality"])
        if in_rarity is None:
            continue
        fv = item["float"]
        title = item["title"]
        item_source = item.get("source", "DMarket")

        # Get max adjusted float for this collection+rarity
        max_adjusted = float_limits.get((coll, in_rarity))
        if max_adjusted is None:
            continue

        # Look up this skin's float range
        skin_name = extract_skin_name(title)
        skin_range = skin_float_ranges.get(skin_name, {"min_float": 0.0, "max_float": 1.0})
        skin_min = skin_range["min_float"]
        skin_max = skin_range["max_float"]

        # Calculate max raw float for this specific skin
        max_raw_float = calc_max_input_float_for_skin(max_adjusted, skin_min, skin_max)
        if max_raw_float is None:
            continue

        # Filter: FT skins with float <= max_raw_float for this skin
        if fv > max_raw_float:
            continue

        # Get price from source
        source_price = item["price_usd"]

        # Compare with Skinport price
        sp_data = skinport_prices.get(title, {})
        skinport_price = sp_data.get("price") if sp_data else None

        if skinport_price and skinport_price < source_price:
            best_price = skinport_price
            best_source = "Skinport"
        else:
            best_price = source_price
            best_source = item_source

        is_stattrak = "StatTrak" in title

        processed.append({
            "title": title,
            "extra": {
                "floatValue": fv,
                "collection": [item["collection"]],
                "quality": item["quality"],
                "skin_min": skin_min,
                "skin_max": skin_max,
            },
            "_best_price": best_price,
            "_best_source": best_source,
            "_is_stattrak": is_stattrak,
        })

    return processed


def phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges, target=15000):
    """Fetch inputs from DMarket + CSFloat, compare with Skinport prices."""
    print("\n" + "=" * 70)
    print("PHASE 1: Fetching input skins from DMarket + CSFloat + Skinport")
    print("=" * 70)

    # === DMARKET ===
    cached_items, cache_time, cache_status = load_dmarket_cache()
    dmarket_items = []

    if cache_status == "fresh":
        age_mins = (time.time() - cache_time) / 60
        print(f"   [DMARKET CACHE] Using fresh cache ({age_mins:.0f}m old, {len(cached_items)} items)")
        dmarket_items = cached_items
    else:
        stale_fallback = cached_items if cache_status == "stale" else None

        print(f"   [DMARKET] Fetching {len(WEAPON_NAMES)} weapon types in parallel...")
        fetched_items = []
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(fetch_weapon_raw, weapon, 2000): weapon
                    for weapon in WEAPON_NAMES
                }
                for future in as_completed(futures):
                    weapon = futures[future]
                    try:
                        items = future.result()
                        fetched_items.extend(items)
                    except Exception as e:
                        print(f"   Error fetching {weapon}: {e}")

            if fetched_items:
                save_dmarket_cache(fetched_items)
                print(f"   [DMARKET] Fetched {len(fetched_items)} raw items (cached for 3h)")
                dmarket_items = fetched_items
            elif stale_fallback:
                age_mins = (time.time() - cache_time) / 60
                print(f"   [DMARKET CACHE] Fetch failed, using stale fallback ({age_mins:.0f}m old)")
                dmarket_items = stale_fallback

        except Exception as e:
            print(f"   DMarket fetch error: {e}")
            if stale_fallback:
                age_mins = (time.time() - cache_time) / 60
                print(f"   [DMARKET CACHE] Using stale fallback ({age_mins:.0f}m old)")
                dmarket_items = stale_fallback

    # === CSFLOAT ===
    csfloat_cached, csfloat_time, csfloat_status = load_csfloat_input_cache()
    csfloat_items = []

    if csfloat_status == "fresh":
        age_mins = (time.time() - csfloat_time) / 60
        print(f"   [CSFLOAT CACHE] Using fresh cache ({age_mins:.0f}m old, {len(csfloat_cached)} items)")
        csfloat_items = csfloat_cached
    else:
        stale_fallback = csfloat_cached if csfloat_status == "stale" else None

        print(f"   [CSFLOAT] Fetching low-float FT listings...")
        try:
            fetched = fetch_csfloat_ft_listings(max_items=2000)
            if fetched:
                save_csfloat_input_cache(fetched)
                print(f"   [CSFLOAT] Fetched {len(fetched)} raw items (cached for 3h)")
                csfloat_items = fetched
            elif stale_fallback:
                age_mins = (time.time() - csfloat_time) / 60
                print(f"   [CSFLOAT CACHE] Fetch failed, using stale fallback ({age_mins:.0f}m old)")
                csfloat_items = stale_fallback
        except Exception as e:
            print(f"   CSFloat fetch error: {e}")
            if stale_fallback:
                csfloat_items = stale_fallback

    # Merge DMarket and CSFloat items
    # Mark DMarket items with source
    for item in dmarket_items:
        if "source" not in item:
            item["source"] = "DMarket"

    raw_items = dmarket_items + csfloat_items
    print(f"   [COMBINED] {len(dmarket_items)} DMarket + {len(csfloat_items)} CSFloat = {len(raw_items)} total")

    if not raw_items:
        print("   No input data available!")
        return {}, {}

    # Process raw items with current float limits and Skinport prices
    all_items = process_cached_items(raw_items, float_limits, skinport_prices, skin_float_ranges)
    print(f"   Total viable inputs (dynamic float ranges): {len(all_items)}")

    # Group by collection + rarity
    by_collection = defaultdict(lambda: defaultdict(list))
    for item in all_items:
        extra = item.get("extra", {})
        coll = extra.get("collection", [])[0].lower().replace("the ", "").replace(" collection", "").strip()
        quality = extra.get("quality", "").lower()
        by_collection[coll][RARITY_ORDER[quality]].append(item)

    # Find viable collections (10+ inputs) and watchlist (5-9 inputs)
    viable = {}
    watchlist = {}
    for coll, rarities in by_collection.items():
        for rarity, items in rarities.items():
            if len(items) >= 10:
                if coll not in viable:
                    viable[coll] = {}
                viable[coll][rarity] = items
            elif len(items) >= 5:
                if coll not in watchlist:
                    watchlist[coll] = {}
                watchlist[coll][rarity] = items

    print(f"   Viable collections (10+ inputs): {len(viable)}")
    print(f"   Watchlist collections (5-9 inputs): {len(watchlist)}")
    return viable, watchlist


# ============ PHASE 2: FETCH OUTPUT PRICES ============

def fetch_skinport_prices():
    """Fetch Skinport prices with caching, rate limiting, and stale fallback."""
    # Check cache first
    cached_prices, cache_time, cache_status = load_skinport_cache()

    if cache_status == "fresh":
        age_mins = (time.time() - cache_time) / 60
        print(f"   [SKINPORT CACHE] Using fresh cache ({age_mins:.0f}m old, {len(cached_prices)} items)")
        return cached_prices

    # Wait for rate limit cooldown before API call
    wait_for_skinport_cooldown()

    # Try to fetch fresh data
    try:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "br, gzip, deflate",
            "User-Agent": "Mozilla/5.0",
        }
        set_skinport_last_call()  # Record call time BEFORE request
        r = requests.get(SKINPORT_URL, params={"app_id": 730, "currency": "USD"},
                        headers=headers, timeout=60)

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "unknown")
            print(f"   WARNING: Skinport rate limited (retry after {retry_after}s)")
            # Fall back to stale cache if available
            if cache_status == "stale" and cached_prices:
                age_mins = (time.time() - cache_time) / 60
                print(f"   [SKINPORT CACHE] Using stale fallback ({age_mins:.0f}m old, {len(cached_prices)} items)")
                return cached_prices
            print(f"   No cache available, continuing with DMarket only...")
            return {}

        if r.ok:
            data = r.json()
            prices = {}
            for item in data:
                name = item.get("market_hash_name", "")
                min_price = item.get("min_price")
                quantity = item.get("quantity", 0)
                if name and min_price is not None:
                    prices[name] = {
                        "price": int(min_price * 100),
                        "quantity": quantity,
                    }
            # Save to cache
            save_skinport_cache(prices)
            print(f"   [SKINPORT] Fetched fresh ({len(prices)} items, cached for 3h)")
            return prices

    except Exception as e:
        print(f"   Error fetching Skinport: {e}")
        # Fall back to stale cache if available
        if cache_status == "stale" and cached_prices:
            age_mins = (time.time() - cache_time) / 60
            print(f"   [SKINPORT CACHE] Using stale fallback ({age_mins:.0f}m old)")
            return cached_prices

    return {}


def get_skinport_price(skinport_prices, skin_name, condition):
    """Look up price from pre-fetched Skinport data."""
    market_hash_name = f"{skin_name} ({condition})"
    price = skinport_prices.get(market_hash_name)
    if price is None:
        return "ERROR"
    return price


def fetch_csfloat_price(skin_name, condition):
    """Fetch lowest CSFloat buy-now price for specific skin + condition."""
    try:
        market_hash_name = f"{skin_name} ({condition})"
        headers = {"Authorization": CSFLOAT_API_KEY}
        params = {
            "market_hash_name": market_hash_name,
            "sort_by": "lowest_price",
            "limit": 1,
            "type": "buy_now",
        }
        r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=10)
        if r.ok:
            data = r.json()
            listings = data.get("data", [])
            if listings:
                # Price is in cents
                return int(listings[0].get("price", 0))
    except:
        pass
    return 0


def phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges):
    """For viable collections, calculate output float and fetch CSFloat prices."""
    print("\n" + "=" * 70)
    print("PHASE 2: Calculating EVs (CSFloat output prices)")
    print("=" * 70)

    results = []
    skipped_supply = 0
    skipped_stattrak = 0
    prices_from_cache = 0
    prices_fetched = 0

    for coll_name, rarities in viable_collections.items():
        if coll_name not in coll_skins:
            continue

        for in_rarity, inputs in rarities.items():
            out_rarity = in_rarity + 1
            outputs = coll_skins[coll_name].get(out_rarity, [])

            if not outputs:
                continue

            # Separate StatTrak and non-StatTrak inputs
            stattrak_inputs = [i for i in inputs if i.get("_is_stattrak", False)]
            normal_inputs = [i for i in inputs if not i.get("_is_stattrak", False)]

            # Use whichever group has more items (prefer non-StatTrak if equal)
            if len(normal_inputs) >= 10:
                selected_inputs = normal_inputs
                is_stattrak_tradeup = False
            elif len(stattrak_inputs) >= 10:
                selected_inputs = stattrak_inputs
                is_stattrak_tradeup = True
            else:
                # Not enough of either type - skip (mixed would be invalid)
                skipped_stattrak += 1
                continue

            # Sort by best price, take 10 cheapest
            selected_inputs.sort(key=lambda x: x.get("_best_price", 999999))
            top10 = selected_inputs[:10]

            # LIQUIDITY CHECK: Verify we have exactly 10 unique listings
            # (DMarket listings are individual items, so len >= 10 means 10+ available)
            if len(selected_inputs) < 10:
                skipped_supply += 1
                continue

            input_cost = sum(x.get("_best_price", 0) for x in top10)

            # Build input data for October 2025 algorithm
            input_data = []
            for x in top10:
                extra = x.get("extra", {})
                input_data.append({
                    "float": extra.get("floatValue", 0.18),
                    "skin_min": extra.get("skin_min", 0.0),
                    "skin_max": extra.get("skin_max", 1.0),
                })
            avg_float = sum(d["float"] for d in input_data) / 10

            # Calculate output conditions FIRST
            output_conditions = []
            for out in outputs:
                out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
                out_cond = get_condition(out_fv)
                output_conditions.append((out["name"], out_cond, out_fv, out["min_float"], out["max_float"]))

            # Look up prices from CSFloat for outputs
            ev_sum = 0
            out_info = []
            has_price = False

            for name, cond, fv, out_min, out_max in output_conditions:
                cache_key = f"{name}|{cond}"

                if cache_key in cached_prices:
                    price = cached_prices[cache_key]
                    prices_from_cache += 1
                else:
                    # Get CSFloat price
                    price = fetch_csfloat_price(name, cond)
                    cached_prices[cache_key] = price
                    prices_fetched += 1
                    time.sleep(0.1)  # Rate limit CSFloat calls

                # Apply CSFloat seller fee
                price_after_fee = int(price * (1 - TOTAL_SELL_FEE)) if price else 0

                if price_after_fee > 0:
                    has_price = True

                prob = 1 / len(outputs)
                ev_sum += price_after_fee * prob
                out_info.append({
                    "name": name,
                    "condition": cond,
                    "float": fv,
                    "float_min": out_min,
                    "float_max": out_max,
                    "price_raw": price,
                    "price_after_fee": price_after_fee,
                    "probability": prob,
                })

            if not has_price:
                continue

            # Calculate EV
            net_ev = ev_sum - input_cost
            roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

            results.append({
                "collection": coll_name,
                "in_rarity": in_rarity,
                "out_rarity": out_rarity,
                "input_cost": input_cost,
                "avg_float": avg_float,
                "ev_output": ev_sum,
                "ev": net_ev,
                "roi": roi,
                "is_stattrak": is_stattrak_tradeup,
                "outputs": out_info,
                "inputs": [{
                    "title": x.get("title"),
                    "price": x.get("_best_price", 0),
                    "source": x.get("_best_source", "?"),
                    "float": x.get("extra", {}).get("floatValue", 0),
                    "skin_min": x.get("extra", {}).get("skin_min", 0),
                    "skin_max": x.get("extra", {}).get("skin_max", 1),
                } for x in top10],
            })

    print(f"   Prices: {prices_fetched} fetched, {prices_from_cache} from cache")
    print(f"   Calculated {len(results)} trade-ups")
    if skipped_supply > 0:
        print(f"   Skipped {skipped_supply} (INSUFFICIENT SUPPLY - need 10+ listings)")
    if skipped_stattrak > 0:
        print(f"   Skipped {skipped_stattrak} (MIXED STATTRAK - can't mix ST and non-ST)")

    return results, cached_prices


def calculate_watchlist_estimates(watchlist_collections, coll_skins, cached_prices):
    """Estimate ROI for watchlist collections (5-9 inputs) if they became executable."""
    watchlist_results = []

    for coll_name, rarities in watchlist_collections.items():
        if coll_name not in coll_skins:
            continue

        for in_rarity, inputs in rarities.items():
            out_rarity = in_rarity + 1
            outputs = coll_skins[coll_name].get(out_rarity, [])

            if not outputs:
                continue

            # Separate StatTrak and non-StatTrak
            stattrak_inputs = [i for i in inputs if i.get("_is_stattrak", False)]
            normal_inputs = [i for i in inputs if not i.get("_is_stattrak", False)]

            # Use whichever group has more
            if len(normal_inputs) >= len(stattrak_inputs):
                selected_inputs = normal_inputs
                is_stattrak = False
            else:
                selected_inputs = stattrak_inputs
                is_stattrak = True

            if len(selected_inputs) < 5:
                continue

            # Sort by price, use what we have
            selected_inputs.sort(key=lambda x: x.get("_best_price", 999999))
            available = selected_inputs[:10]  # Take up to 10

            # Estimate input cost (extrapolate if < 10)
            if len(available) > 0:
                avg_price = sum(x.get("_best_price", 0) for x in available) / len(available)
                estimated_input_cost = int(avg_price * 10)
            else:
                continue

            # Build input data for October 2025 algorithm
            input_data = []
            for x in available:
                extra = x.get("extra", {})
                input_data.append({
                    "float": extra.get("floatValue", 0.18),
                    "skin_min": extra.get("skin_min", 0.0),
                    "skin_max": extra.get("skin_max", 1.0),
                })
            avg_float = sum(d["float"] for d in input_data) / len(input_data)

            # Calculate estimated output value using cached prices
            ev_sum = 0
            has_price = False
            for out in outputs:
                out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
                out_cond = get_condition(out_fv)
                cache_key = f"{out['name']}|{out_cond}"

                price = cached_prices.get(cache_key, 0)
                price_after_fee = int(price * (1 - TOTAL_SELL_FEE)) if price else 0

                if price_after_fee > 0:
                    has_price = True

                prob = 1 / len(outputs)
                ev_sum += price_after_fee * prob

            if not has_price:
                # No cached prices, skip
                continue

            net_ev = ev_sum - estimated_input_cost
            roi = (net_ev / estimated_input_cost * 100) if estimated_input_cost > 0 else 0

            # Count sources
            sources = defaultdict(int)
            for inp in available:
                sources[inp.get("_best_source", "?")] += 1

            watchlist_results.append({
                "collection": coll_name,
                "in_rarity": in_rarity,
                "out_rarity": out_rarity,
                "inputs_found": len(selected_inputs),
                "inputs_needed": 10 - len(selected_inputs),
                "estimated_input_cost": estimated_input_cost,
                "estimated_ev": net_ev,
                "estimated_roi": roi,
                "is_stattrak": is_stattrak,
                "sources": dict(sources),
            })

    watchlist_results.sort(key=lambda x: x["estimated_roi"], reverse=True)
    return watchlist_results


# ============ PHASE 3: FETCH TRENDS FOR PROFITABLE ============

def fetch_steam_trend(skin_name, condition):
    """Fetch Steam 30-day trend data."""
    try:
        market_hash_name = f"{skin_name} ({condition})"
        params = {"appid": 730, "currency": 1, "market_hash_name": market_hash_name}
        r = requests.get(STEAM_URL, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok:
            data = r.json()
            if data.get("success"):
                result = {}
                lowest = data.get("lowest_price", "")
                if lowest:
                    result["lowest"] = int(float(lowest.replace("$", "").replace(",", "")) * 100)
                median = data.get("median_price", "")
                if median:
                    result["median"] = int(float(median.replace("$", "").replace(",", "")) * 100)
                result["volume_24h"] = int(data.get("volume", "0").replace(",", "") or 0)
                return result
    except:
        pass
    return None


def phase3_fetch_trends(profitable_results):
    """Fetch Steam trends only for profitable trade-ups."""
    print("\n" + "=" * 70)
    print("PHASE 3: Fetching Steam trends for profitable trade-ups")
    print("=" * 70)

    if not profitable_results:
        print("   No profitable trade-ups to analyze")
        return profitable_results

    print(f"   Fetching trends for {len(profitable_results)} profitable trade-ups...")

    for result in profitable_results:
        for out in result["outputs"]:
            trend = fetch_steam_trend(out["name"], out["condition"])
            if trend:
                # Adjust median for 15% Steam fee
                if trend.get("median"):
                    trend["median_after_fee"] = int(trend["median"] * (1 - STEAM_SELLER_FEE))
                out["steam_trend"] = trend
            time.sleep(0.1)  # Rate limit

    print("   Done fetching trends")
    return profitable_results


# ============ MAIN ============

def main():
    print("=" * 70)
    print("CS2 TRADE-UP EV CALCULATOR v3.1")
    print("Inputs: DMarket+CSFloat+Skinport | Outputs: CSFloat")
    print("=" * 70)

    # Load skin database
    print("\nLoading CS2 skin database...")
    skins_data = requests.get(SKINS_URL).json()

    coll_skins = defaultdict(lambda: defaultdict(list))
    skin_float_ranges = {}  # skin_name -> {min_float, max_float}

    for skin in skins_data:
        category = skin.get("category", {}).get("name", "")
        if category not in VALID_CATEGORIES:
            continue
        # Note: souvenir field means "souvenir version exists", not "only souvenir"
        # Souvenir items are filtered from DMarket listings, not here

        rarity = skin.get("rarity", {}).get("name", "").lower()
        if rarity not in RARITY_ORDER:
            continue

        collections = skin.get("collections", [])
        if not collections:
            continue

        name = skin.get("name", "")
        min_float = skin.get("min_float") or 0.0
        max_float = skin.get("max_float") or 1.0

        # Store float range lookup by skin name
        skin_float_ranges[name] = {"min_float": min_float, "max_float": max_float}

        for coll in collections:
            coll_name = coll.get("name", "").lower().replace("the ", "").replace(" collection", "").strip()
            coll_skins[coll_name][RARITY_ORDER[rarity]].append({
                "name": name,
                "min_float": min_float,
                "max_float": max_float,
            })

    print(f"Loaded {len(coll_skins)} collections")

    # Build dynamic float limits per collection+rarity
    float_limits = build_input_float_limits(coll_skins)
    print(f"Calculated float limits for {len(float_limits)} collection+rarity combos")

    # Fetch Skinport prices (used for both inputs and outputs)
    print("\nLoading Skinport prices...")
    skinport_prices = fetch_skinport_prices()

    # Load cache
    cached_prices, cache_time = load_cache()
    if cached_prices and is_cache_valid(cache_time):
        age_mins = (time.time() - cache_time) / 60
        print(f"[CACHE] Loaded {len(cached_prices)} prices ({age_mins:.0f}m old, valid for 3h)")
    else:
        cached_prices = {}
        print("[CACHE] Starting fresh")

    # PHASE 1: Fetch inputs
    viable_collections, watchlist_collections = phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges)

    if not viable_collections and not watchlist_collections:
        print("\nNo viable or watchlist collections found")
        return

    # PHASE 2: Calculate EVs
    results, cached_prices = phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges)
    save_cache(cached_prices)

    # Sort by ROI, filter for 25%+ ROI only
    MIN_ROI = 25.0
    results.sort(key=lambda x: x["roi"], reverse=True)
    profitable = [r for r in results if r["ev"] > 0 and r["roi"] >= MIN_ROI]

    # PHASE 3: Fetch trends for profitable only
    profitable = phase3_fetch_trends(profitable)

    # Display results
    print("\n" + "=" * 70)
    all_profitable = [r for r in results if r["ev"] > 0]
    print(f"RESULTS: {len(results)} trade-ups analyzed, {len(all_profitable)} profitable, {len(profitable)} with ROI >= {MIN_ROI}%")
    print("=" * 70)

    if profitable:
        print("\n*** PROFITABLE TRADE-UPS ***\n")
        for r in profitable[:10]:
            print("=" * 80)
            st_tag = " [STATTRAK]" if r.get("is_stattrak") else ""
            print(f"  [{r['collection'].upper()}]{st_tag} +{r['roi']:.1f}% ROI | EV: ${r['ev']/100:.2f}")
            print("=" * 80)
            print(f"  Rarity: {RARITY_NAMES[r['in_rarity']].upper()} -> {RARITY_NAMES[r['out_rarity']].upper()}")
            print(f"  Total Input Cost: ${r['input_cost']/100:.2f}")
            print(f"  Average Input Float: {r['avg_float']:.4f}")
            print(f"  Expected Output Value: ${r['ev_output']/100:.2f}")
            print(f"  Net Profit (EV): ${r['ev']/100:.2f}")

            # Count MW vs FT outputs
            mw_count = sum(1 for o in r["outputs"] if o["condition"] == "Minimal Wear")
            ft_count = len(r["outputs"]) - mw_count
            print(f"  Output Conditions: {mw_count} MW, {ft_count} FT")

            print("\n  " + "-" * 76)
            print("  POSSIBLE OUTPUTS:")
            print("  " + "-" * 76)
            for out in r["outputs"]:
                print(f"\n    [{out['probability']*100:.0f}% chance] {out['name']}")
                print(f"    Condition: {out['condition']} (output float: {out['float']:.4f})")
                print(f"    Float Range: {out['float_min']:.2f} - {out['float_max']:.2f}", end="")
                if out['condition'] == "Field-Tested":
                    print(" << MW IMPOSSIBLE (range too wide)")
                else:
                    print(" (MW achievable)")

                if out['price_raw'] > 0:
                    print(f"    CSFloat Price: ${out['price_raw']/100:.2f} (${out['price_after_fee']/100:.2f} after 2% fee)")
                    # Generate CSFloat search link
                    search_name = out['name'].replace(' ', '%20').replace('|', '%7C')
                    print(f"    Sell on CSFloat: https://csfloat.com/search?name={search_name}")
                else:
                    print(f"    CSFloat Price: No listing found")

                # Steam trend
                trend = out.get("steam_trend", {})
                if trend:
                    parts = []
                    if trend.get("median"):
                        parts.append(f"Steam Median: ${trend['median']/100:.2f}")
                    if trend.get("median_after_fee"):
                        parts.append(f"After 15% fee: ${trend['median_after_fee']/100:.2f}")
                    if trend.get("volume_24h"):
                        parts.append(f"Volume: {trend['volume_24h']}/day")
                    if parts:
                        print(f"    Steam: {' | '.join(parts)}")

            print("\n  " + "-" * 76)
            print("  ALL 10 INPUTS (buy these):")
            print("  " + "-" * 76)
            print(f"  {'#':<3} {'Price':>7} {'Float':>8} {'Source':<10} {'Item Name'}")
            print(f"  {'-'*3} {'-'*7} {'-'*8} {'-'*10} {'-'*45}")
            for i, inp in enumerate(r["inputs"], 1):
                print(f"  {i:<3} ${inp['price']/100:>6.2f} {inp['float']:>8.4f} {inp['source']:<10} {inp['title']}")

            # Generate buy links grouped by skin and source
            print(f"\n  " + "-" * 76)
            print("  BUY LINKS (direct to listings):")
            print("  " + "-" * 76)

            # Group inputs by base skin name
            skins_by_source = {}
            for inp in r["inputs"]:
                base_name = inp["title"].replace(" (Field-Tested)", "")
                market_hash = inp["title"]  # Full name with condition
                if base_name not in skins_by_source:
                    skins_by_source[base_name] = {"sources": set(), "max_float": 0, "market_hash": ""}
                skins_by_source[base_name]["sources"].add(inp["source"])
                skins_by_source[base_name]["max_float"] = max(skins_by_source[base_name]["max_float"], inp["float"])
                skins_by_source[base_name]["market_hash"] = market_hash

            # Determine category filter (1=normal, 2=stattrak)
            category = "2" if r.get("is_stattrak") else "1"

            for skin_name, data in skins_by_source.items():
                print(f"\n    {skin_name}:")
                search_name = skin_name.replace(' ', '%20').replace('|', '%7C')
                market_hash = data["market_hash"].replace(' ', '%20').replace('|', '%7C')
                max_flt = min(data["max_float"] + 0.01, 0.25)  # Slightly above max found, cap at 0.25

                # Skinport link with filters: sort by price, FT condition
                print(f"      Skinport: https://skinport.com/market/730?search={search_name}&sort=price&order=asc&exterior=2")
                # CSFloat link with all filters: float range, buy_now only, normal/stattrak category, sort by price
                print(f"      CSFloat:  https://csfloat.com/search?market_hash_name={market_hash}&min_float=0.15&max_float={max_flt:.2f}&sort_by=lowest_price&type=buy_now&category={category}")

            # Output sell links with expected float ranges
            print(f"\n  " + "-" * 76)
            print("  SELL LINKS (check current prices for outputs):")
            print("  " + "-" * 76)
            for out in r["outputs"]:
                out_name = f"{out['name']} ({out['condition']})"
                out_hash = out_name.replace(' ', '%20').replace('|', '%7C')
                out_float = out.get("float", 0)
                # Float range: from output float to condition max (with small buffer)
                if out["condition"] == "Factory New":
                    float_max = 0.07
                elif out["condition"] == "Minimal Wear":
                    float_max = 0.15
                else:
                    float_max = 0.38
                float_min = max(0, out_float - 0.01)
                float_max_filter = min(float_max, out_float + 0.02)

                print(f"    {out['name']} ({out['condition'][:2]}) - expected float ~{out_float:.4f}:")
                print(f"      https://csfloat.com/search?market_hash_name={out_hash}&min_float={float_min:.2f}&max_float={float_max_filter:.2f}&sort_by=lowest_price&type=buy_now&category={category}")

            print()
            print("=" * 80)
            print()
    else:
        print("\nNo profitable trade-ups found.")
        print("\nTop 5 closest to profitable:\n")
        for r in results[:5]:
            print(f"[{r['roi']:+.1f}%] {r['collection']} | {RARITY_NAMES[r['in_rarity']]} -> {RARITY_NAMES[r['out_rarity']]}")
            print(f"    Input: ${r['input_cost']/100:.2f}, EV: ${r['ev']/100:.2f}")
            print()

    # WATCH LIST: Collections with 5-9 inputs (close to executable)
    if watchlist_collections:
        watchlist_results = calculate_watchlist_estimates(watchlist_collections, coll_skins, cached_prices)

        if watchlist_results:
            print("\n" + "=" * 70)
            print("WATCH LIST: Collections with 5-9 inputs (need more to execute)")
            print("=" * 70)
            print(f"\n{'Collection':<25} {'Rarity':<25} {'Found':>6} {'Need':>5} {'Est.ROI':>8} {'Sources'}")
            print("-" * 90)

            for w in watchlist_results[:15]:
                rarity_str = f"{RARITY_NAMES[w['in_rarity']][:3]}->{RARITY_NAMES[w['out_rarity']][:3]}"
                st_tag = " [ST]" if w.get("is_stattrak") else ""
                sources_str = ", ".join(f"{k}:{v}" for k, v in w["sources"].items())

                print(f"{w['collection']:<25} {rarity_str:<25} {w['inputs_found']:>6} {w['inputs_needed']:>5} {w['estimated_roi']:>+7.1f}% {sources_str}")

            print("\n   * Est.ROI assumes similar prices for remaining inputs")
            print("   * Enable Skinport to find more listings and potentially complete these")


if __name__ == "__main__":
    main()
