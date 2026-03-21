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
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY", "skYpZbif0-zYaiAA1nxlQmwL1AsGAZrN")
STEAM_URL = "https://steamcommunity.com/market/priceoverview/"
SKINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "price_cache.json")
SKINPORT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "skinport_cache.json")
DMARKET_CACHE_FILE = os.path.join(os.path.dirname(__file__), "dmarket_cache.json")
CSFLOAT_INPUT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "csfloat_input_cache.json")
SKINPORT_RATELIMIT_FILE = os.path.join(os.path.dirname(__file__), "skinport_lastcall.txt")
OPPORTUNITIES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "opportunities_cache.json")
CACHE_EXPIRY = 3 * 60 * 60  # 3 hours for output prices
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours (keep stale data as fallback)
INPUT_CACHE_EXPIRY = 6 * 60 * 60  # 6 hours for input listings (don't change fast)
INPUT_CACHE_STALE_EXPIRY = 12 * 60 * 60  # 12 hours stale fallback for input listings
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
    # Must be >= skin's minimum float to be achievable
    return max_raw if max_raw >= skin_min else None


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
    except (IOError, json.JSONDecodeError, ValueError):
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
            try:
                os.remove(SKINPORT_CACHE_FILE)
            except OSError:
                pass
            return {}, 0, "expired"
    except (IOError, json.JSONDecodeError, ValueError):
        return {}, 0, "error"


def save_skinport_cache(prices):
    """Save Skinport prices to cache."""
    data = {"timestamp": time.time(), "prices": prices}
    with open(SKINPORT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_dmarket_cache():
    """Load DMarket cache with stale fallback support (6h fresh for inputs)."""
    if not os.path.exists(DMARKET_CACHE_FILE):
        return [], 0, "none"
    try:
        with open(DMARKET_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < INPUT_CACHE_EXPIRY:
            return data.get("items", []), timestamp, "fresh"
        elif age < INPUT_CACHE_STALE_EXPIRY:  # 12h stale fallback
            return data.get("items", []), timestamp, "stale"
        else:
            # Too old, delete it
            try:
                os.remove(DMARKET_CACHE_FILE)
            except OSError:
                pass
            return [], 0, "expired"
    except (IOError, json.JSONDecodeError, ValueError):
        return [], 0, "error"


def save_dmarket_cache(items):
    """Save DMarket listings to cache."""
    data = {"timestamp": time.time(), "items": items}
    with open(DMARKET_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_csfloat_input_cache():
    """Load CSFloat input listings cache with stale fallback support (6h fresh for inputs)."""
    if not os.path.exists(CSFLOAT_INPUT_CACHE_FILE):
        return [], 0, "none"
    try:
        with open(CSFLOAT_INPUT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < INPUT_CACHE_EXPIRY:
            return data.get("items", []), timestamp, "fresh"
        elif age < INPUT_CACHE_STALE_EXPIRY:  # 12h stale fallback
            return data.get("items", []), timestamp, "stale"
        else:
            try:
                os.remove(CSFLOAT_INPUT_CACHE_FILE)
            except OSError:
                pass
            return [], 0, "expired"
    except (IOError, json.JSONDecodeError, ValueError):
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
    except (IOError, ValueError):
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


# ============ OPPORTUNITIES CACHE ============

def load_opportunities():
    """Load saved profitable opportunities."""
    if not os.path.exists(OPPORTUNITIES_CACHE_FILE):
        return []
    try:
        with open(OPPORTUNITIES_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("opportunities", [])
    except (IOError, json.JSONDecodeError, ValueError):
        return []


def save_opportunities(opportunities):
    """Save profitable opportunities to cache."""
    data = {"timestamp": time.time(), "opportunities": opportunities}
    with open(OPPORTUNITIES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def verify_csfloat_listing(listing_id):
    """Check if a specific CSFloat listing still exists and get current price."""
    try:
        headers = {"Authorization": CSFLOAT_API_KEY}
        url = f"{CSFLOAT_URL}/{listing_id}"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "available": True,
                "price": data.get("price", 0),
                "float": data.get("item", {}).get("float_value", 0),
            }
        elif r.status_code == 404:
            return {"available": False, "reason": "sold or removed"}
        else:
            return {"available": False, "reason": f"API error {r.status_code}"}
    except Exception as e:
        return {"available": False, "reason": str(e)}


def verify_opportunity(opportunity, cached_prices):
    """Verify a saved opportunity is still valid.

    Returns: (status, updated_opportunity)
    - status: 'confirmed', 'expired', 'price_changed'
    - updated_opportunity: opportunity with current prices or None if expired
    """
    inputs = opportunity.get("inputs", [])
    collection = opportunity.get("collection", "")

    available_inputs = []
    unavailable_count = 0
    total_new_cost = 0

    print(f"   Verifying {collection}...")

    for inp in inputs:
        listing_id = inp.get("listing_id", "")
        source = inp.get("source", "")

        if source == "CSFloat" and listing_id:
            result = verify_csfloat_listing(listing_id)
            time.sleep(0.2)  # Rate limit

            if result["available"]:
                available_inputs.append({
                    **inp,
                    "current_price": result["price"],
                    "price_changed": result["price"] != inp.get("price", 0),
                })
                total_new_cost += result["price"]
            else:
                unavailable_count += 1
        else:
            # DMarket/Skinport listings - can't verify individually, assume available
            # but mark as unverified
            available_inputs.append({
                **inp,
                "current_price": inp.get("price", 0),
                "unverified": True,
            })
            total_new_cost += inp.get("price", 0)

    # Need all 10 inputs available
    if unavailable_count > 0:
        print(f"      {unavailable_count} listings no longer available")
        return "expired", None

    # Recalculate profitability with current prices
    # Get output EV from cached prices
    outputs = opportunity.get("outputs", [])
    ev_sum = 0
    for out in outputs:
        cache_key = f"{out['name']}|{out['condition']}"
        price = cached_prices.get(cache_key, out.get("price_raw", 0))
        price_after_fee = int(price * (1 - CSFLOAT_SELLER_FEE)) if price else 0
        ev_sum += price_after_fee * out["probability"]

    net_ev = ev_sum - total_new_cost
    roi = (net_ev / total_new_cost * 100) if total_new_cost > 0 else 0

    # Check if still profitable
    if net_ev < 30 or roi < 25.0:
        print(f"      No longer profitable (ROI: {roi:.1f}%, EV: ${net_ev/100:.2f})")
        return "expired", None

    # Update opportunity with current data
    updated = {
        **opportunity,
        "inputs": available_inputs,
        "input_cost": total_new_cost,
        "ev": net_ev,
        "roi": roi,
        "verified_at": time.time(),
    }

    print(f"      CONFIRMED: ROI {roi:.1f}%, EV ${net_ev/100:.2f}")
    return "confirmed", updated


def verify_saved_opportunities(cached_prices):
    """Check all saved opportunities and return confirmed/expired status."""
    opportunities = load_opportunities()

    if not opportunities:
        return [], []

    print("\n" + "=" * 70)
    print("VERIFYING SAVED OPPORTUNITIES")
    print("=" * 70)

    confirmed = []
    expired = []

    for opp in opportunities:
        status, updated = verify_opportunity(opp, cached_prices)
        if status == "confirmed":
            confirmed.append(updated)
        else:
            expired.append(opp)

    # Save only confirmed opportunities back
    if confirmed:
        save_opportunities(confirmed)
    elif os.path.exists(OPPORTUNITIES_CACHE_FILE):
        try:
            os.remove(OPPORTUNITIES_CACHE_FILE)
        except OSError:
            pass

    print(f"   Confirmed: {len(confirmed)}, Expired: {len(expired)}")

    return confirmed, expired


def save_profitable_opportunities(profitable_results):
    """Save profitable trade-ups to opportunities cache."""
    if not profitable_results:
        return

    opportunities = []
    for r in profitable_results:
        # Build complete opportunity record
        opp = {
            "collection": r["collection"],
            "in_rarity": r["in_rarity"],
            "out_rarity": r["out_rarity"],
            "is_stattrak": r.get("is_stattrak", False),
            "input_cost": r["input_cost"],
            "ev_output": r["ev_output"],
            "ev": r["ev"],
            "roi": r["roi"],
            "avg_float": r["avg_float"],
            "max_adjusted": r.get("max_adjusted"),
            "saved_at": time.time(),
            "inputs": [],
            "outputs": r["outputs"],
        }

        # Save full input details
        for inp in r["inputs"]:
            source = inp.get("source", "DMarket")
            listing_id = inp.get("listing_id", "")

            # Build URL based on source
            if source == "CSFloat" and listing_id:
                url = f"https://csfloat.com/item/{listing_id}"
            elif source == "DMarket" and listing_id:
                url = f"https://dmarket.com/ingame-items/item-list/csgo-skins?userOfferId={listing_id}"
            else:
                url = ""

            opp["inputs"].append({
                "title": inp.get("title", ""),
                "price": inp.get("price", 0),
                "float": inp.get("float", 0),
                "adjusted_float": inp.get("adjusted_float", 0),
                "skin_min": inp.get("skin_min", 0),
                "skin_max": inp.get("skin_max", 1),
                "max_float": inp.get("max_float"),
                "source": source,
                "listing_id": listing_id,
                "url": url,
            })

        opportunities.append(opp)

    save_opportunities(opportunities)
    print(f"\n   Saved {len(opportunities)} opportunities to {OPPORTUNITIES_CACHE_FILE}")


# ============ PHASE 1: FETCH INPUTS ============

WEAPON_NAMES = [
    "AK-47", "M4A4", "M4A1-S", "AWP", "Desert Eagle", "USP-S", "Glock-18",
    "P250", "Five-SeveN", "Tec-9", "CZ75-Auto", "Dual Berettas", "R8 Revolver",
    "P2000", "MP9", "MAC-10", "MP7", "UMP-45", "P90", "PP-Bizon", "MP5-SD",
    "FAMAS", "Galil AR", "SG 553", "AUG", "SSG 08", "SCAR-20", "G3SG1",
    "Nova", "XM1014", "MAG-7", "Sawed-Off", "M249", "Negev"
]


def fetch_csfloat_listings(max_items=2000):
    """Fetch all condition listings from CSFloat sorted by price."""
    all_items = []
    seen_listing_ids = set()  # Track seen listing IDs to avoid duplicates
    headers = {"Authorization": CSFLOAT_API_KEY}

    # Fetch skins using price-based pagination (page param doesn't work properly)
    # Use category=1 for normal skins (excludes knives, gloves, etc. that can't trade up)
    min_price = 0  # Start from $0
    max_iterations = 50  # Safety limit

    for iteration in range(max_iterations):
        if len(all_items) >= max_items:
            break

        params = {
            "sort_by": "lowest_price",
            "type": "buy_now",
            "category": 1,  # Normal skins only
            "limit": 50,
            "min_price": min_price,
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

            new_items_this_batch = 0
            max_price_seen = min_price
            for listing in listings:
                item = listing.get("item", {})
                fv = item.get("float_value")
                market_hash_name = item.get("market_hash_name", "")

                # Skip souvenir
                if "Souvenir" in market_hash_name:
                    continue

                if fv is None:
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

                # Get listing ID for direct link
                listing_id = listing.get("id", "")

                # Skip if we've already seen this listing
                if listing_id in seen_listing_ids:
                    continue
                seen_listing_ids.add(listing_id)
                new_items_this_batch += 1

                # Track highest price to use as next min_price
                if price > max_price_seen:
                    max_price_seen = price

                all_items.append({
                    "title": market_hash_name,
                    "price_usd": price,
                    "float": fv,
                    "collection": coll_name,
                    "quality": rarity,
                    "source": "CSFloat",
                    "listing_id": listing_id,
                })

            # If no new items and price didn't change, we've exhausted listings
            if new_items_this_batch == 0:
                break

            # Use same price tier to catch remaining items at this price
            # (dedup via seen_listing_ids prevents counting them twice)
            min_price = max_price_seen
            time.sleep(CSFLOAT_INPUT_RATE_LIMIT)

        except Exception as e:
            print(f"   [CSFLOAT] Error: {e}")
            break

    return all_items


def fetch_weapon_raw(weapon_name, max_items=500):
    """Fetch raw items for a weapon from DMarket (all conditions)."""
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

                # Get item ID for direct link
                item_id = item.get("itemId", "")

                all_items.append({
                    "title": title,
                    "price_usd": int(item.get("price", {}).get("USD", 0)),
                    "float": fv,
                    "collection": colls[0].lower().replace("the ", "").replace(" collection", "").strip(),
                    "quality": quality,
                    "listing_id": item_id,
                })

            cursor = data.get("cursor")
            if not cursor:
                break
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"   WARNING: DMarket fetch interrupted ({e}), got {len(all_items)} items so far")
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
        skin_range = skin_float_ranges.get(skin_name)
        if skin_range is None:
            # Unknown skin - skip to avoid incorrect float calculations
            continue
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

        # Always use real float from DMarket/CSFloat listing
        # But if Skinport has cheaper price, use that price
        if skinport_price and skinport_price < source_price:
            best_price = skinport_price
            price_from_skinport = True
        else:
            best_price = source_price
            price_from_skinport = False

        is_stattrak = "StatTrak" in title

        processed.append({
            "title": title,
            "extra": {
                "floatValue": fv,  # Real float from DMarket/CSFloat
                "collection": [item["collection"]],
                "quality": item["quality"],
                "skin_min": skin_min,
                "skin_max": skin_max,
            },
            "_best_price": best_price,
            "_best_source": item_source,  # Always the real listing source
            "_price_from_skinport": price_from_skinport,
            "_is_stattrak": is_stattrak,
            "_listing_id": item.get("listing_id", ""),
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
                print(f"   [DMARKET] Fetched {len(fetched_items)} raw items (cached for 6h)")
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

        print(f"   [CSFLOAT] Fetching listings (all conditions)...")
        try:
            fetched = fetch_csfloat_listings(max_items=2000)
            if fetched:
                save_csfloat_input_cache(fetched)
                print(f"   [CSFLOAT] Fetched {len(fetched)} raw items (cached for 6h)")
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

    # Group by collection + rarity, deduplicating by listing_id
    by_collection = defaultdict(lambda: defaultdict(list))
    seen_by_coll_rarity = defaultdict(set)  # Track seen listing_ids per collection+rarity
    duplicates_skipped = 0

    for item in all_items:
        extra = item.get("extra", {})
        colls = extra.get("collection", [])
        if not colls:
            continue
        coll = colls[0].lower().replace("the ", "").replace(" collection", "").strip()
        quality = extra.get("quality", "").lower()
        if quality not in RARITY_ORDER:
            continue
        listing_id = item.get("_listing_id", "")

        # Generate fallback dedup key when listing_id is missing
        if not listing_id:
            fv = extra.get("floatValue", 0)
            price = item.get("_best_price", 0)
            title = item.get("title", "")
            listing_id = f"_synth_{title}_{price}_{fv}"

        # Skip duplicates within same collection+rarity
        key = (coll, RARITY_ORDER[quality])
        if listing_id in seen_by_coll_rarity[key]:
            duplicates_skipped += 1
            continue
        seen_by_coll_rarity[key].add(listing_id)

        by_collection[coll][RARITY_ORDER[quality]].append(item)

    if duplicates_skipped > 0:
        print(f"   Duplicates skipped: {duplicates_skipped}")

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
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
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

            # Validate: warn if no outputs achieve MW (the entire thesis is FT->MW)
            mw_outputs = sum(1 for o in out_info if o["condition"] == "Minimal Wear" or o["condition"] == "Factory New")
            if mw_outputs == 0:
                # All outputs are FT or worse - no MW benefit, skip
                continue

            # Calculate EV
            net_ev = ev_sum - input_cost
            roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

            # Calculate max allowed float for each input skin type
            # Find the most restrictive max_adjusted across all outputs
            min_max_adjusted = None
            for out in outputs:
                max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"])
                if max_adj is not None:
                    if min_max_adjusted is None or max_adj < min_max_adjusted:
                        min_max_adjusted = max_adj

            # Build inputs with max_raw_float calculated
            inputs_with_limits = []
            for x in top10:
                extra = x.get("extra", {})
                skin_min = extra.get("skin_min", 0)
                skin_max = extra.get("skin_max", 1)
                skin_range = skin_max - skin_min
                raw_float = extra.get("floatValue", 0)
                adjusted_float = (raw_float - skin_min) / skin_range if skin_range > 0 else 0
                max_raw = calc_max_input_float_for_skin(min_max_adjusted, skin_min, skin_max) if min_max_adjusted else None
                inputs_with_limits.append({
                    "title": x.get("title"),
                    "price": x.get("_best_price", 0),
                    "source": x.get("_best_source", "?"),
                    "price_from_skinport": x.get("_price_from_skinport", False),
                    "float": raw_float,
                    "adjusted_float": adjusted_float,
                    "skin_min": skin_min,
                    "skin_max": skin_max,
                    "max_float": max_raw,
                    "listing_id": x.get("_listing_id", ""),
                })

            results.append({
                "collection": coll_name,
                "in_rarity": in_rarity,
                "out_rarity": out_rarity,
                "input_cost": input_cost,
                "avg_float": avg_float,
                "max_adjusted": min_max_adjusted,
                "ev_output": ev_sum,
                "ev": net_ev,
                "roi": roi,
                "is_stattrak": is_stattrak_tradeup,
                "outputs": out_info,
                "inputs": inputs_with_limits,
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
                known_cost = sum(x.get("_best_price", 0) for x in available)
                missing = 10 - len(available)
                if missing > 0:
                    # Use max price of available items for missing ones (pessimistic)
                    max_price = max(x.get("_best_price", 0) for x in available)
                    estimated_input_cost = round(known_cost + missing * max_price)
                else:
                    estimated_input_cost = round(known_cost)
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
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
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

    # Check saved opportunities first
    confirmed_opps, expired_opps = verify_saved_opportunities(cached_prices)

    # PHASE 1: Fetch inputs
    viable_collections, watchlist_collections = phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges)

    if not viable_collections and not watchlist_collections:
        print("\nNo viable or watchlist collections found")
        return

    # PHASE 2: Calculate EVs
    results, cached_prices = phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges)
    save_cache(cached_prices)

    # Sort by ROI, filter for 25%+ ROI and $0.30+ net profit
    MIN_ROI = 25.0
    MIN_EV = 30  # $0.30 in cents
    results.sort(key=lambda x: x["roi"], reverse=True)
    profitable = [r for r in results if r["ev"] > 0 and r["roi"] >= MIN_ROI and r["ev"] >= MIN_EV]

    # PHASE 3: Fetch trends for profitable only
    profitable = phase3_fetch_trends(profitable)

    # Save profitable opportunities
    save_profitable_opportunities(profitable)

    # Display confirmed opportunities from previous runs
    if confirmed_opps:
        print("\n" + "=" * 70)
        print(f"CONFIRMED OPPORTUNITIES ({len(confirmed_opps)} from previous scan)")
        print("=" * 70)
        for opp in confirmed_opps:
            st_tag = " [STATTRAK]" if opp.get("is_stattrak") else ""
            print(f"\n  [{opp['collection'].upper()}]{st_tag} +{opp['roi']:.1f}% ROI | EV: ${opp['ev']/100:.2f}")
            print(f"  Input Cost: ${opp['input_cost']/100:.2f} | Verified: {time.strftime('%H:%M', time.localtime(opp.get('verified_at', 0)))}")
            print("  BUY LINKS:")
            for inp in opp["inputs"][:5]:
                if inp.get("url"):
                    print(f"    ${inp['price']/100:.2f} | {inp['title'][:40]}")
                    print(f"      {inp['url']}")
            if len(opp["inputs"]) > 5:
                print(f"    ... and {len(opp['inputs'])-5} more")

    if expired_opps:
        print(f"\n  [{len(expired_opps)} opportunities expired (listings sold or prices changed)]")

    # Display results
    print("\n" + "=" * 70)
    all_profitable = [r for r in results if r["ev"] > 0]
    meets_roi = [r for r in results if r["ev"] > 0 and r["roi"] >= MIN_ROI]
    print(f"RESULTS: {len(results)} trade-ups analyzed, {len(all_profitable)} profitable, {len(meets_roi)} with ROI >= {MIN_ROI}%, {len(profitable)} with EV >= ${MIN_EV/100:.2f}")
    print("=" * 70)

    if profitable:
        print("\n*** PROFITABLE TRADE-UPS ***\n")
        for r in profitable[:10]:
            print("=" * 80)
            st_tag = " [STATTRAK]" if r.get("is_stattrak") else ""
            print(f"  [{r['collection'].upper()}]{st_tag} +{r['roi']:.1f}% ROI | EV: ${r['ev']/100:.2f}")
            print("=" * 80)
            print(f"  Rarity: {RARITY_NAMES[r['in_rarity']].upper()} -> {RARITY_NAMES[r['out_rarity']].upper()}")
            print(f"  Total Input Cost: ${r['input_cost']/100:.2f} (10 skins)")
            print(f"  Average Input Float: {r['avg_float']:.4f} (raw)")
            # Calculate average adjusted float
            avg_adj = sum(inp.get("adjusted_float", 0) for inp in r["inputs"]) / len(r["inputs"]) if r["inputs"] else 0
            print(f"  Average Adjusted Float: {avg_adj:.4f} (normalized)")
            print(f"  Max Adjusted Allowed: {r.get('max_adjusted', 0):.4f} (for MW output)")
            print(f"  Expected Output Value: ${r['ev_output']/100:.2f} (after 2% CSFloat fee)")
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
                print(f"    Condition: {out['condition']} (expected output float: {out['float']:.4f})")
                print(f"    Skin Float Range: {out['float_min']:.2f} - {out['float_max']:.2f}", end="")
                if out['condition'] == "Field-Tested":
                    print(" << MW IMPOSSIBLE (range too wide)")
                else:
                    # Calculate max adjusted that would give MW
                    max_adj_for_mw = calc_max_adjusted_float(out['float_min'], out['float_max'])
                    if max_adj_for_mw:
                        print(f" (MW needs avg_adj < {max_adj_for_mw:.4f})")
                    else:
                        print(" (MW achievable)")

                if out['price_raw'] > 0:
                    print(f"    CSFloat Price: ${out['price_raw']/100:.2f} (${out['price_after_fee']/100:.2f} after 2% fee)")
                    # EV contribution from this output
                    ev_contribution = out['price_after_fee'] * out['probability']
                    print(f"    EV Contribution: ${ev_contribution/100:.2f} ({out['probability']*100:.0f}% × ${out['price_after_fee']/100:.2f})")
                    # Generate CSFloat search link
                    search_name = out['name'].replace(' ', '%20').replace('|', '%7C')
                    print(f"    Sell on CSFloat: https://csfloat.com/search?market_hash_name={search_name}%20%28{out['condition'].replace(' ', '%20')}%29&sort_by=lowest_price")
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
            print("  INPUT REQUIREMENTS (October 2025 Algorithm):")
            print("  " + "-" * 76)

            # Get unique input skins and their max float limits
            input_skins = {}
            for inp in r["inputs"]:
                base_name = inp["title"].replace(" (Field-Tested)", "")
                if base_name not in input_skins:
                    input_skins[base_name] = {
                        "max_float": inp.get("max_float", 0.25),
                        "skin_min": inp["skin_min"],
                        "skin_max": inp["skin_max"],
                        "count": 0,
                        "floats": [],
                        "adjusted_floats": [],
                    }
                input_skins[base_name]["count"] += 1
                input_skins[base_name]["floats"].append(inp["float"])
                input_skins[base_name]["adjusted_floats"].append(inp.get("adjusted_float", 0))

            # Use overall avg_adj across all 10 inputs (matches EV calculation)
            overall_avg_adj = sum(inp.get("adjusted_float", 0) for inp in r["inputs"]) / len(r["inputs"]) if r["inputs"] else 0

            for skin_name, data in input_skins.items():
                avg_flt = sum(data["floats"]) / len(data["floats"])
                max_flt = data["max_float"]

                print(f"\n  {skin_name}:")
                print(f"    Buy: {data['count']}x Field-Tested (max float: {max_flt:.4f})")

            # Show output conditions using overall avg across all 10 inputs
            overall_avg_flt = sum(inp["float"] for inp in r["inputs"]) / len(r["inputs"]) if r["inputs"] else 0
            print(f"\n  Output conditions at overall avg float {overall_avg_flt:.4f} (adj {overall_avg_adj:.4f}):")
            current_outputs = []
            for out in r["outputs"]:
                out_fv = out["float_min"] + overall_avg_adj * (out["float_max"] - out["float_min"])
                if out_fv < 0.07:
                    cond = "FN"
                elif out_fv < 0.15:
                    cond = "MW"
                else:
                    cond = "FT"
                name_parts = out['name'].split('|')
                short_name = name_parts[1].strip()[:10] if len(name_parts) > 1 else name_parts[0].strip()[:10]
                current_outputs.append(f"{short_name}={cond}")
            print(f"    {', '.join(current_outputs)}")

            print("\n  " + "-" * 76)
            print("  EXACT LISTINGS USED IN CALCULATION:")
            print("  " + "-" * 76)
            print(f"  {'#':<3} {'Price':>7} {'Float':>8} {'Adjusted':>9} {'MaxFloat':>9} {'Source':<18} {'Skin'}")
            print(f"  {'-'*3} {'-'*7} {'-'*8} {'-'*9} {'-'*9} {'-'*18} {'-'*25}")
            has_skinport_price = False
            for i, inp in enumerate(r["inputs"], 1):
                max_flt = inp.get("max_float", 0)
                adj_flt = inp.get("adjusted_float", 0)
                skin_short = inp["title"].replace(" (Field-Tested)", "")[:25]
                source = inp.get("source", "?")
                if inp.get("price_from_skinport", False):
                    has_skinport_price = True
                    source_str = f"{source} (SP$)"
                else:
                    source_str = source
                print(f"  {i:<3} ${inp['price']/100:>6.2f} {inp['float']:>8.4f} {adj_flt:>9.4f} {max_flt:>9.4f} {source_str:<18} {skin_short}")

            # Calculate and show average adjusted float
            avg_adj = sum(inp.get("adjusted_float", 0) for inp in r["inputs"]) / len(r["inputs"])
            avg_raw = sum(inp.get("float", 0) for inp in r["inputs"]) / len(r["inputs"])
            print(f"\n  AVERAGE: Raw Float = {avg_raw:.4f}, Adjusted Float = {avg_adj:.4f}")
            if has_skinport_price:
                sp_count = sum(1 for inp in r["inputs"] if inp.get("price_from_skinport", False))
                print(f"  * (SP$) = {sp_count} items use Skinport price (cheaper), float from DMarket/CSFloat listing")

            # Direct links to exact listings (grouped by skin to reduce redundancy)
            print("\n  " + "-" * 76)
            print("  DIRECT LINKS TO LISTINGS:")
            print("  " + "-" * 76)

            # Group by skin name and track unique listings
            skin_listings = {}
            for inp in r["inputs"]:
                base_name = inp["title"].replace(" (Field-Tested)", "")
                original_source = inp.get("original_source", inp.get("source", "?"))
                listing_id = inp.get("listing_id", "")

                if base_name not in skin_listings:
                    skin_listings[base_name] = {
                        "count": 0,
                        "listings": [],  # List of (source, listing_id, price, float)
                        "market_hash": inp["title"],
                        "max_float": inp.get("max_float", 0.25),
                        "best_price_source": inp.get("source", "?"),
                    }
                skin_listings[base_name]["count"] += 1

                # Track unique listings (by listing_id)
                if listing_id and listing_id not in [l[1] for l in skin_listings[base_name]["listings"]]:
                    skin_listings[base_name]["listings"].append(
                        (original_source, listing_id, inp["price"], inp["float"])
                    )

            for skin_name, data in skin_listings.items():
                print(f"\n  {skin_name} ({data['count']}x needed):")

                # Show direct listing links if we have them
                if data["listings"]:
                    print(f"    Direct listings from DMarket/CSFloat:")
                    for src, lid, price, flt in data["listings"][:5]:  # Show max 5
                        if src == "CSFloat":
                            print(f"      ${price/100:.2f} @ {flt:.4f}: https://csfloat.com/item/{lid}")
                        elif src == "DMarket":
                            print(f"      ${price/100:.2f} @ {flt:.4f}: https://dmarket.com/ingame-items/item-list/csgo-skins?userOfferId={lid}")
                    if len(data["listings"]) > 5:
                        print(f"      ... and {len(data['listings']) - 5} more")

                # Show search links
                market_hash = data["market_hash"].replace(' ', '%20').replace('|', '%7C')
                max_flt = data["max_float"]
                print(f"    Search (max float {max_flt:.4f}):")
                print(f"      Skinport: https://skinport.com/market/730?search={market_hash}&sort=price&order=asc&exterior=2")
                print(f"      CSFloat:  https://csfloat.com/search?market_hash_name={market_hash}&max_float={max_flt:.4f}&sort_by=lowest_price&type=buy_now")
                print(f"      DMarket:  https://dmarket.com/ingame-items/item-list/csgo-skins?title={market_hash.replace('%20', '+').replace('%7C', '%257C')}&exterior=field-tested")


            # Output sell links with expected float ranges
            print(f"\n  " + "-" * 76)
            print("  SELL LINKS (check current prices for outputs):")
            print("  " + "-" * 76)
            # Determine category filter (1=normal, 2=stattrak)
            category = "2" if r.get("is_stattrak") else "1"
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
