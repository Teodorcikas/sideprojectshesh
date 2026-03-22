import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import sys
import json
import os
import time
import threading


class RateLimiter:
    """Token-bucket rate limiter. Enforces a minimum interval between calls globally across all threads."""
    def __init__(self, max_per_second):
        self._interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()


_csfloat_rate_limiter = RateLimiter(max_per_second=2)  # 500ms between every CSFloat request globally

# Global CSFloat budget tracker — updated from response headers
_csfloat_budget = {"remaining": None, "limit": None, "reset": None, "lock": threading.Lock()}

def _update_csfloat_budget(response):
    """Update budget tracker from CSFloat response headers."""
    with _csfloat_budget["lock"]:
        try:
            _csfloat_budget["remaining"] = int(response.headers.get("X-RateLimit-Remaining", -1))
            _csfloat_budget["limit"] = int(response.headers.get("X-RateLimit-Limit", -1))
            reset = response.headers.get("X-RateLimit-Reset")
            if reset:
                _csfloat_budget["reset"] = int(reset)
        except (ValueError, TypeError):
            pass

def _csfloat_has_budget():
    """Return True if we have enough CSFloat budget to make a request."""
    with _csfloat_budget["lock"]:
        remaining = _csfloat_budget["remaining"]
        if remaining is None:
            return True  # Unknown budget, allow (first request will tell us)
        return remaining > CSFLOAT_BUDGET_RESERVE

sys.stdout.reconfigure(encoding='utf-8')

DMARKET_URL = "https://api.dmarket.com/exchange/v1/market/items"
SKINPORT_URL = "https://api.skinport.com/v1/items"
CSFLOAT_URL = "https://csfloat.com/api/v1/listings"
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY", "skYpZbif0-zYaiAA1nxlQmwL1AsGAZrN")
WAXPEER_URL = "https://api.waxpeer.com/v1/get-items-list"
WAXPEER_API_KEY = os.environ.get("WAXPEER_API_KEY", "")  # Set your Waxpeer API key here
STEAM_URL = "https://steamcommunity.com/market/priceoverview/"
SKINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "price_cache.json")
SKINPORT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "skinport_cache.json")
DMARKET_CACHE_FILE = os.path.join(os.path.dirname(__file__), "dmarket_cache.json")
CSFLOAT_INPUT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "csfloat_input_cache.json")
WAXPEER_CACHE_FILE = os.path.join(os.path.dirname(__file__), "waxpeer_cache.json")
SKINPORT_RATELIMIT_FILE = os.path.join(os.path.dirname(__file__), "skinport_lastcall.txt")
OPPORTUNITIES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "opportunities_cache.json")
WINNERS_FILE = os.path.join(os.path.dirname(__file__), "winners.md")
CACHE_EXPIRY = 3 * 60 * 60  # 3 hours for output prices
CACHE_STALE_EXPIRY = 6 * 60 * 60  # 6 hours (keep stale data as fallback)
INPUT_CACHE_EXPIRY = 6 * 60 * 60  # 6 hours for input listings (don't change fast)
INPUT_CACHE_STALE_EXPIRY = 12 * 60 * 60  # 12 hours stale fallback for input listings
SKINPORT_COOLDOWN = 60  # 1 minute between Skinport API calls
CSFLOAT_BUDGET_RESERVE = 10  # Keep 10 requests in reserve for testing/debugging


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
    """Calculate max adjusted input float to achieve target output float.

    Returns max allowed average adjusted float for the given target condition threshold.
    """
    if out_max <= out_min:
        return None
    max_adjusted = (target_output - out_min) / (out_max - out_min)
    return max_adjusted if max_adjusted > 0 else None


# Output condition targets: (name, max_output_float)
# FN < 0.07, MW < 0.15, FT < 0.38
OUTPUT_TARGETS = [
    ("Factory New", 0.07),
    ("Minimal Wear", 0.15),
    ("Field-Tested", 0.38),
]


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

    For each collection+rarity, calculates limits for all output conditions (FN, MW, FT).
    Returns the MOST PERMISSIVE limit (FT threshold) so the widest range of inputs qualifies.
    The actual output condition is determined later in Phase 2 based on the real avg float.
    """
    limits = {}  # (collection, input_rarity) -> max_adjusted_float

    for coll_name, rarities in coll_skins.items():
        for out_rarity, skins in rarities.items():
            in_rarity = out_rarity - 1  # Input is one rarity below output
            if in_rarity < 0:
                continue

            # Find the most permissive limit across all output targets (FN, MW, FT)
            # and all output skins. We want: for the most restrictive output skin,
            # what's the highest adjusted float that still lands in at least FT?
            best_limit = None
            for target_name, target_float in OUTPUT_TARGETS:
                # For this target, find the most restrictive skin
                min_limit_for_target = None
                for skin in skins:
                    out_min = skin.get("min_float", 0.0)
                    out_max = skin.get("max_float", 1.0)
                    limit = calc_max_adjusted_float(out_min, out_max, target_float)
                    if limit is not None:
                        if min_limit_for_target is None or limit < min_limit_for_target:
                            min_limit_for_target = limit

                # Use the most permissive target that works
                if min_limit_for_target is not None:
                    if best_limit is None or min_limit_for_target > best_limit:
                        best_limit = min_limit_for_target

            if best_limit is not None:
                limits[(coll_name, in_rarity)] = best_limit

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


def load_waxpeer_cache():
    """Load Waxpeer cache with stale fallback support (6h fresh for inputs)."""
    if not os.path.exists(WAXPEER_CACHE_FILE):
        return [], 0, "none"
    try:
        with open(WAXPEER_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        if age < INPUT_CACHE_EXPIRY:
            return data.get("items", []), timestamp, "fresh"
        elif age < INPUT_CACHE_STALE_EXPIRY:
            return data.get("items", []), timestamp, "stale"
        else:
            try:
                os.remove(WAXPEER_CACHE_FILE)
            except OSError:
                pass
            return [], 0, "expired"
    except (IOError, json.JSONDecodeError, ValueError):
        return [], 0, "error"


def save_waxpeer_cache(items):
    """Save Waxpeer listings to cache."""
    data = {"timestamp": time.time(), "items": items}
    with open(WAXPEER_CACHE_FILE, "w", encoding="utf-8") as f:
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


def append_winners_log(profitable_results):
    """Append profitable trade-ups to the persistent winners.md log."""
    if not profitable_results:
        return

    from datetime import date
    today = date.today().isoformat()

    lines = [f"\n## {today}\n"]
    for r in profitable_results:
        coll = r["collection"].title()
        in_r = RARITY_NAMES[r["in_rarity"]].title()
        out_r = RARITY_NAMES[r["out_rarity"]].title()
        roi = r["roi"]
        ev = r["ev"] / 100
        cost = r["input_cost"] / 100
        ev_out = r["ev_output"] / 100
        avg_flt = r["avg_float"]
        adj_flt = r.get("max_adjusted", 0)

        input_skin = r["inputs"][0]["title"] if r["inputs"] else "?"
        input_price = r["inputs"][0]["price"] / 100 if r["inputs"] else 0
        max_flt = r["inputs"][0].get("max_float") or 0

        lines.append(f"### {coll} | {in_r} → {out_r} | +{roi:.1f}% ROI | EV: ${ev:.2f}")
        lines.append(f"- **Input:** 10x {input_skin} @ ${input_price:.2f} each = ${cost:.2f} total")
        lines.append(f"- **Max input float:** {max_flt:.4f}")
        lines.append(f"- **Avg input float:** {avg_flt:.4f} (max adjusted allowed: {adj_flt:.4f})")
        lines.append(f"- **Expected output value:** ${ev_out:.2f} (after platform fees)")
        lines.append(f"- **Outputs (1/{len(r['outputs'])} each):**")
        for o in r["outputs"]:
            source = o.get("price_source", "CSFloat")
            price = o["price_raw"] / 100
            jackpot = " ← jackpot" if o["price_raw"] >= 1000 else ""
            lines.append(f"  - {o['name']} ({o['condition'][:2]}, ~{o['float']:.4f}) — ${price:.2f} [{source}]{jackpot}")
        lines.append("")

    lines.append("---\n")
    entry = "\n".join(lines)

    with open(WINNERS_FILE, "a", encoding="utf-8") as f:
        f.write(entry)

    print(f"   Appended {len(profitable_results)} winner(s) to winners.md")


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
    append_winners_log(profitable_results)


# ============ PHASE 1: FETCH INPUTS ============



def fetch_csfloat_listings(float_limits, max_pages_per_rarity=40):
    """Fetch input listings from CSFloat using server-side filters.

    Uses rarity + max_float filters so every result is useful (zero waste).
    CSFloat API: limit=50 max, rarity=int (1-6), max_float=float, category=1.

    Budget strategy: up to 40 pages × ~5 rarities = ~200 requests.
    Output prices use Steam (free), so CSFloat budget goes to inputs.
    Budget guard stops when reserve (10) is reached.
    """
    # Map our rarity names to CSFloat integer codes
    RARITY_TO_CSFLOAT = {
        "consumer grade": 1, "industrial grade": 2, "mil-spec grade": 3,
        "restricted": 4, "classified": 5, "covert": 6,
    }

    # Figure out which rarities we need and the max float for each
    # float_limits is keyed by (collection, rarity_int) -> max_adjusted_float
    # We need the broadest max_float across all collections for each rarity
    # (server-side filter is global, client-side will refine per-collection)
    rarity_max_floats = {}  # rarity_int -> max raw float (conservative upper bound)
    for (coll, in_rarity), max_adjusted in float_limits.items():
        # max_adjusted is normalized 0-1. Convert to a raw float upper bound.
        # Worst case: skin with range 0.0-1.0 → max_raw = max_adjusted.
        # For safety, use 0.38 (FT ceiling) as absolute cap since we want FT or better.
        raw_upper = min(max_adjusted, 0.38)
        if in_rarity not in rarity_max_floats or raw_upper > rarity_max_floats[in_rarity]:
            rarity_max_floats[in_rarity] = raw_upper

    if not rarity_max_floats:
        print("   [CSFLOAT] No rarities to fetch")
        return []

    all_items = []
    seen_listing_ids = set()
    headers = {"Authorization": CSFLOAT_API_KEY}
    total_requests = 0

    for rarity_int, max_float in sorted(rarity_max_floats.items()):
        rarity_name = RARITY_NAMES[rarity_int] if rarity_int < len(RARITY_NAMES) else f"rarity_{rarity_int}"
        csfloat_rarity = RARITY_TO_CSFLOAT.get(rarity_name, rarity_int + 1)

        # Check budget before starting this rarity
        with _csfloat_budget["lock"]:
            remaining = _csfloat_budget["remaining"]
        if remaining is not None and remaining <= CSFLOAT_BUDGET_RESERVE:
            print(f"   [CSFLOAT] Budget low ({remaining} left, reserve={CSFLOAT_BUDGET_RESERVE}), stopping input fetch")
            break

        min_price = 0
        rarity_items = 0

        for page in range(max_pages_per_rarity):
            # Check budget EVERY page, not just per rarity
            with _csfloat_budget["lock"]:
                remaining = _csfloat_budget["remaining"]
            if remaining is not None and remaining <= CSFLOAT_BUDGET_RESERVE:
                print(f"   [CSFLOAT] Budget reserve hit ({remaining} left), stopping {rarity_name}")
                break

            params = {
                "sort_by": "lowest_price",
                "type": "buy_now",
                "category": 1,
                "limit": 50,
                "min_price": min_price,
                "max_float": max_float,
                "rarity": csfloat_rarity,
            }

            try:
                _csfloat_rate_limiter.wait()
                r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=15)
                _update_csfloat_budget(r)
                total_requests += 1

                if r.status_code == 429:
                    with _csfloat_budget["lock"]:
                        rl_reset = _csfloat_budget["reset"]
                    wait_time = 10
                    if rl_reset:
                        wait_time = max(1, rl_reset - int(time.time()) + 1)
                        if wait_time > 300:
                            print(f"   [CSFLOAT 429] {rarity_name} p{page+1} | reset in {wait_time}s (>5m, stopping)")
                            break
                    print(f"   [CSFLOAT 429] {rarity_name} p{page+1} | waiting {wait_time}s")
                    time.sleep(wait_time)
                    continue
                if not r.ok:
                    print(f"   [CSFLOAT] {rarity_name} API error: {r.status_code}")
                    break

                listings = r.json().get("data", [])
                if not listings:
                    break  # No more results for this rarity

                new_this_page = 0
                max_price_seen = min_price
                for listing in listings:
                    item = listing.get("item", {})
                    price = listing.get("price", 0)

                    if price > max_price_seen:
                        max_price_seen = price

                    fv = item.get("float_value")
                    market_hash_name = item.get("market_hash_name", "")

                    if "Souvenir" in market_hash_name or fv is None:
                        continue

                    collection = item.get("collection")
                    if not collection:
                        continue
                    coll_name = collection.lower().replace("the ", "").replace(" collection", "").strip()

                    rarity = item.get("rarity_name", "").lower()
                    if rarity not in RARITY_ORDER:
                        continue

                    listing_id = listing.get("id", "")
                    if listing_id in seen_listing_ids:
                        continue
                    seen_listing_ids.add(listing_id)
                    new_this_page += 1

                    all_items.append({
                        "title": market_hash_name,
                        "price_usd": price,
                        "float": fv,
                        "collection": coll_name,
                        "quality": rarity,
                        "source": "CSFloat",
                        "listing_id": listing_id,
                    })

                rarity_items += new_this_page

                # Advance price cursor
                if max_price_seen > min_price:
                    min_price = max_price_seen
                else:
                    min_price += 1

                # If page was not full, we've exhausted this rarity
                if len(listings) < 50:
                    break

            except Exception as e:
                print(f"   [CSFLOAT] {rarity_name} error: {e}")
                break

        with _csfloat_budget["lock"]:
            remaining = _csfloat_budget["remaining"]
        print(f"   [CSFLOAT] {rarity_name}: {rarity_items} items (max_float={max_float:.4f}) [{remaining}/{_csfloat_budget.get('limit','?')} budget left]")

    print(f"   [CSFLOAT] Total: {len(all_items)} items, {total_requests} API requests used")
    return all_items


def fetch_skin_raw(skin_name, max_items=1000):
    """Fetch all DMarket listings for a specific skin name (all conditions, all prices)."""
    all_items = []
    cursor = None

    while len(all_items) < max_items:
        params = {
            "gameId": "a8db",
            "title": skin_name,
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

                if "Souvenir" in title:
                    continue
                if fv is None or not colls or quality not in RARITY_ORDER:
                    continue

                price_raw = int(item.get("price", {}).get("USD", 0))
                if price_raw <= 0:
                    continue

                item_id = item.get("itemId", "")
                all_items.append({
                    "title": title,
                    "price_usd": price_raw,
                    "float": fv,
                    "collection": colls[0].lower().replace("the ", "").replace(" collection", "").strip(),
                    "quality": quality,
                    "listing_id": item_id,
                })

            cursor = data.get("cursor")
            if not cursor:
                break
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"   WARNING: DMarket fetch interrupted for '{skin_name}' ({e}), got {len(all_items)} so far")
            break

    return all_items


def fetch_waxpeer_listings(skin_names, skin_db_lookup):
    """Fetch Waxpeer listings for given skin names.

    skin_db_lookup: dict of skin_name -> list of {collection, quality} from skin database.
    Returns items in the same normalized format as DMarket/CSFloat.
    """
    if not WAXPEER_API_KEY:
        print("   [WAXPEER] No API key set — skipping (set WAXPEER_API_KEY)")
        return []

    all_items = []
    seen_ids = set()
    total_requests = 0

    for skin_name in skin_names:
        # Waxpeer search by name, sorted cheapest first
        skip = 0
        skin_items = 0
        for page in range(5):  # Max 5 pages (500 items) per skin
            params = {
                "api": WAXPEER_API_KEY,
                "game": "csgo",
                "search": skin_name,
                "order_by": "price",
                "order": "ASC",
                "skip": skip,
            }
            try:
                r = requests.get(WAXPEER_URL, params=params, timeout=15)
                total_requests += 1

                if r.status_code == 429:
                    print(f"   [WAXPEER] Rate limited, stopping")
                    return all_items
                if not r.ok:
                    break

                data = r.json()
                if not data.get("success"):
                    break

                items = data.get("items", [])
                if not items:
                    break

                for item in items:
                    item_name = item.get("name", "")
                    price = item.get("price", 0)  # cents
                    fv = item.get("float")
                    item_id = item.get("item_id", "")

                    if not fv or price <= 0:
                        continue
                    if "Souvenir" in item_name:
                        continue
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    # Extract base name to look up collection+rarity from skin database
                    base_name = extract_skin_name(item_name)
                    db_entries = skin_db_lookup.get(base_name, [])
                    if not db_entries:
                        continue

                    # A skin can belong to multiple collections — add to each
                    for entry in db_entries:
                        all_items.append({
                            "title": item_name,
                            "price_usd": price,
                            "float": fv,
                            "collection": entry["collection"],
                            "quality": entry["quality"],
                            "source": "Waxpeer",
                            "listing_id": f"waxpeer_{item_id}",
                        })
                    skin_items += 1

                skip += 100
                if len(items) < 100:
                    break  # Last page

                time.sleep(0.5)  # Respect rate limit
            except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
                print(f"   WARNING: Waxpeer fetch error for '{skin_name}': {e}")
                break

    print(f"   [WAXPEER] Fetched {len(all_items)} items, {total_requests} API requests")
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
    float_violations = 0
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

        # Hard filter: reject any item where float exceeds max_raw_float
        if fv > max_raw_float + 0.0001:  # tiny epsilon for float rounding
            continue

        # Assertion check — should never trigger after the filter above
        if fv > max_raw_float + 0.0001:
            float_violations += 1

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

    if float_violations > 0:
        print(f"   [BUG] {float_violations} items passed with float > max_raw_float!")
    return processed


def phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges, coll_skins, target=15000):
    """Fetch inputs from DMarket + CSFloat + Waxpeer, compare with Skinport prices."""
    print("\n" + "=" * 70)
    print("PHASE 1: Fetching input skins from DMarket + CSFloat + Waxpeer + Skinport")
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

        # Build deduplicated set of every input skin across all viable collection+rarity combos.
        # This ensures Mil-Spec, Restricted, and Classified skins are fetched directly
        # instead of being crowded out by cheap consumer-grade skins from a global weapon search.
        skins_to_fetch = set()
        for (coll, in_rarity) in float_limits:
            for skin in coll_skins.get(coll, {}).get(in_rarity, []):
                skins_to_fetch.add(skin["name"])

        print(f"   [DMARKET] Fetching {len(skins_to_fetch)} specific skins across all rarity tiers in parallel...")
        fetched_items = []
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(fetch_skin_raw, skin_name): skin_name
                    for skin_name in skins_to_fetch
                }
                for future in as_completed(futures):
                    skin_name = futures[future]
                    try:
                        items = future.result()
                        fetched_items.extend(items)
                    except Exception as e:
                        print(f"   Error fetching '{skin_name}': {e}")

            if fetched_items:
                save_dmarket_cache(fetched_items)
                print(f"   [DMARKET] Fetched {len(fetched_items)} raw items across {len(skins_to_fetch)} skins (cached for 6h)")
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

        print(f"   [CSFLOAT] Fetching listings (targeted by rarity + float)...")
        try:
            fetched = fetch_csfloat_listings(float_limits)
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

    # === WAXPEER ===
    waxpeer_cached, waxpeer_time, waxpeer_status = load_waxpeer_cache()
    waxpeer_items = []

    if not WAXPEER_API_KEY:
        print(f"   [WAXPEER] No API key set — skipping (set WAXPEER_API_KEY in code or env)")
    elif waxpeer_status == "fresh":
        age_mins = (time.time() - waxpeer_time) / 60
        print(f"   [WAXPEER CACHE] Using fresh cache ({age_mins:.0f}m old, {len(waxpeer_cached)} items)")
        waxpeer_items = waxpeer_cached
    else:
        stale_fallback = waxpeer_cached if waxpeer_status == "stale" else None

        # Build skin_db_lookup: base_name -> [{collection, quality}]
        skin_db_lookup = defaultdict(list)
        for coll_name, rarities in coll_skins.items():
            for rarity_int, skins in rarities.items():
                quality = RARITY_NAMES[rarity_int] if rarity_int < len(RARITY_NAMES) else str(rarity_int)
                for skin in skins:
                    skin_db_lookup[skin["name"]].append({
                        "collection": coll_name,
                        "quality": quality,
                    })

        # Fetch same skin set as DMarket
        skins_to_fetch = set()
        for (coll, in_rarity) in float_limits:
            for skin in coll_skins.get(coll, {}).get(in_rarity, []):
                skins_to_fetch.add(skin["name"])

        print(f"   [WAXPEER] Fetching {len(skins_to_fetch)} skins...")
        try:
            fetched = fetch_waxpeer_listings(skins_to_fetch, skin_db_lookup)
            if fetched:
                save_waxpeer_cache(fetched)
                print(f"   [WAXPEER] Got {len(fetched)} items (cached for 6h)")
                waxpeer_items = fetched
            elif stale_fallback:
                age_mins = (time.time() - waxpeer_time) / 60
                print(f"   [WAXPEER CACHE] Fetch failed, using stale fallback ({age_mins:.0f}m old)")
                waxpeer_items = stale_fallback
        except Exception as e:
            print(f"   Waxpeer fetch error: {e}")
            if stale_fallback:
                waxpeer_items = stale_fallback

    # Merge all input sources
    for item in dmarket_items:
        if "source" not in item:
            item["source"] = "DMarket"

    raw_items = dmarket_items + csfloat_items + waxpeer_items
    print(f"   [COMBINED] {len(dmarket_items)} DMarket + {len(csfloat_items)} CSFloat + {len(waxpeer_items)} Waxpeer = {len(raw_items)} total")

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
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "br, gzip, deflate",  # brotli required by Skinport (needs brotli package)
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://skinport.com",
            "Referer": "https://skinport.com/",
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

        if not r.ok:
            print(f"   WARNING: Skinport returned HTTP {r.status_code}")
            if cache_status == "stale" and cached_prices:
                age_mins = (time.time() - cache_time) / 60
                print(f"   [SKINPORT CACHE] Using stale fallback ({age_mins:.0f}m old)")
                return cached_prices
            return {}

        if not r.text.strip():
            print(f"   WARNING: Skinport returned empty response body (HTTP {r.status_code})")
            if cache_status == "stale" and cached_prices:
                age_mins = (time.time() - cache_time) / 60
                print(f"   [SKINPORT CACHE] Using stale fallback ({age_mins:.0f}m old)")
                return cached_prices
            return {}

        data = r.json()
        prices = {}
        for item in data:
            name = item.get("market_hash_name", "")
            min_price = item.get("min_price")
            quantity = item.get("quantity", 0)
            if name and min_price is not None:
                prices[name] = {
                    "price": round(min_price * 100),
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


CSFLOAT_NO_LISTING = -1  # Sentinel: CSFloat confirmed zero active listings for this skin

def fetch_csfloat_price(skin_name, condition):
    """Fetch lowest CSFloat buy-now price for a skin+condition.

    Returns:
      price > 0          — CSFloat has an active listing at this price (cents)
      CSFLOAT_NO_LISTING — CSFloat responded 200 OK but zero listings; skin not sellable here
      0                  — Rate-limited after all retries, or network error; do NOT cache

    Uses the global _csfloat_rate_limiter (4 req/s) before every attempt.
    Retries on 429 with exponential backoff: 5s, 10s, 20s, 40s.
    """
    if not _csfloat_has_budget():
        return 0  # Budget exhausted — don't waste remaining reserve
    market_hash_name = f"{skin_name} ({condition})"
    headers = {"Authorization": CSFLOAT_API_KEY}
    params = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": 1,
        "type": "buy_now",
    }
    max_attempts = 5
    for attempt in range(max_attempts):
        _csfloat_rate_limiter.wait()
        try:
            r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=10)
            _update_csfloat_budget(r)
            if r.status_code == 429:
                rl_remaining = _csfloat_budget["remaining"]
                rl_limit = _csfloat_budget["limit"]
                rl_reset = _csfloat_budget["reset"]
                # Use X-RateLimit-Reset timestamp if available, otherwise fixed backoff
                wait_time = 5
                if rl_reset:
                    wait_time = max(1, rl_reset - int(time.time()) + 1)
                    # Cap wait to 5 minutes — if reset is further out, give up
                    if wait_time > 300:
                        print(f"   [CSFLOAT 429] attempt {attempt+1}/{max_attempts} | {rl_remaining}/{rl_limit} remaining, reset in {wait_time}s (>5m, giving up)")
                        return 0
                else:
                    wait_time = min(5 * (2 ** attempt), 60)
                print(f"   [CSFLOAT 429] attempt {attempt+1}/{max_attempts} | {rl_remaining}/{rl_limit} remaining | waiting {wait_time}s until reset")
                time.sleep(wait_time)
                continue
            if r.status_code in (401, 403):
                print(f"   [CSFLOAT] Auth error {r.status_code} for '{market_hash_name}' — check API key")
                return 0  # Don't retry auth errors
            if r.ok:
                listings = r.json().get("data", [])
                if listings:
                    return int(listings[0].get("price", 0))
                else:
                    return CSFLOAT_NO_LISTING  # Confirmed: CSFloat has no listing for this skin
            # Other non-OK status (5xx etc.) — treat as transient, retry
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
            pass  # Network error — retry
    return 0  # All retries exhausted — do NOT cache this result


def phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges):
    """For viable collections, calculate output float and fetch CSFloat prices."""
    print("\n" + "=" * 70)
    print("PHASE 2: Calculating EVs (CSFloat output prices)")
    print("=" * 70)

    results = []
    skipped_supply = 0
    skipped_stattrak = 0
    skipped_no_outputs = 0
    skipped_no_price = 0
    skipped_not_mw = 0
    prices_from_cache = 0
    prices_fetched = 0
    rarity_label = lambda r: RARITY_NAMES[r] if r < len(RARITY_NAMES) else str(r)

    # --- PRE-FETCH: Collect all output (name, cond) pairs and bulk-fetch in parallel ---
    # Show CSFloat budget status from Phase 1
    with _csfloat_budget["lock"]:
        remaining = _csfloat_budget["remaining"]
        limit = _csfloat_budget["limit"]
    if remaining is not None:
        print(f"\n   [CSFLOAT BUDGET] {remaining}/{limit} requests remaining in current window")
    print(f"\n[PHASE 2] Pre-fetching output prices...")
    all_output_pairs = set()
    for coll_name, rarities in viable_collections.items():
        if coll_name not in coll_skins:
            continue
        for in_rarity, inputs in rarities.items():
            out_rarity = in_rarity + 1
            outputs = coll_skins[coll_name].get(out_rarity, [])
            if not outputs:
                continue
            stattrak_inputs = [i for i in inputs if i.get("_is_stattrak", False)]
            normal_inputs = [i for i in inputs if not i.get("_is_stattrak", False)]
            if len(normal_inputs) >= 10:
                selected = normal_inputs
            elif len(stattrak_inputs) >= 10:
                selected = stattrak_inputs
            else:
                continue
            selected.sort(key=lambda x: x.get("_best_price", 999999))
            top10 = selected[:10]
            if len(selected) < 10:
                continue
            input_data = []
            for x in top10:
                extra = x.get("extra", {})
                input_data.append({
                    "float": extra.get("floatValue", 0.18),
                    "skin_min": extra.get("skin_min", 0.0),
                    "skin_max": extra.get("skin_max", 1.0),
                })
            for out in outputs:
                out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
                out_cond = get_condition(out_fv)
                cache_key = f"{out['name']}|{out_cond}"
                if cache_key not in cached_prices:
                    all_output_pairs.add((out["name"], out_cond))

    # Track price sources: cache_key -> "Steam" / "Skinport" / "CSFloat"
    price_sources = {}

    if all_output_pairs:
        # OUTPUT PRICING STRATEGY:
        # CSFloat budget is spent on INPUTS (where exact float matters).
        # For outputs, condition is what matters — use free sources:
        #   1. Steam median/lowest (free, based on actual sales) — sell fee 15%
        #   2. Skinport (free, already fetched in bulk) — sell fee 8%
        #   3. CSFloat only if budget allows — sell fee 2%

        # STEP 1: Steam prices (free, unlimited, most reliable)
        steam_ok = 0
        steam_no_data = 0
        print(f"   Fetching {len(all_output_pairs)} output prices from Steam (free)...")
        for name, cond in all_output_pairs:
            cache_key = f"{name}|{cond}"
            if cache_key in cached_prices:
                continue
            trend = fetch_steam_trend(name, cond)
            time.sleep(0.35)  # Steam rate limit ~3 req/s
            if trend:
                steam_ref = trend.get("median") or trend.get("lowest")
                if steam_ref and steam_ref > 0:
                    cached_prices[cache_key] = steam_ref
                    price_sources[cache_key] = "Steam"
                    steam_ok += 1
                else:
                    steam_no_data += 1
            else:
                steam_no_data += 1
        print(f"   Steam: {steam_ok} priced, {steam_no_data} no data")

        # STEP 2: Skinport fallback for Steam misses (free, already fetched)
        skinport_filled = 0
        still_missing = []
        for name, cond in all_output_pairs:
            cache_key = f"{name}|{cond}"
            if cached_prices.get(cache_key, 0) > 0:
                continue
            sp_data = get_skinport_price(skinport_prices, name, cond)
            if sp_data != "ERROR" and sp_data.get("quantity", 0) >= 2 and sp_data.get("price", 0) > 0:
                cached_prices[cache_key] = sp_data["price"]
                price_sources[cache_key] = "Skinport"
                skinport_filled += 1
            else:
                still_missing.append((name, cond))
        if skinport_filled:
            print(f"   Skinport fallback: {skinport_filled} filled")

        # STEP 3: CSFloat last resort — only for skins with no Steam/Skinport price
        csfloat_ok = 0
        if still_missing and _csfloat_has_budget():
            with _csfloat_budget["lock"]:
                remaining = _csfloat_budget["remaining"]
            print(f"   CSFloat last resort for {len(still_missing)} missing prices ({remaining} budget left)...")

            def _fetch_output_price(name_cond):
                name, cond = name_cond
                return name_cond, fetch_csfloat_price(name, cond)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(_fetch_output_price, pair): pair for pair in still_missing}
                for future in as_completed(futures):
                    (name, cond), price = future.result()
                    if price > 0:
                        cached_prices[f"{name}|{cond}"] = price
                        price_sources[f"{name}|{cond}"] = "CSFloat"
                        csfloat_ok += 1
                    elif price == CSFLOAT_NO_LISTING:
                        cached_prices[f"{name}|{cond}"] = CSFLOAT_NO_LISTING
        elif still_missing:
            print(f"   {len(still_missing)} outputs have no price (CSFloat budget too low to check)")

        has_price = sum(1 for v in cached_prices.values() if v > 0)
        no_listing = sum(1 for v in cached_prices.values() if v == CSFLOAT_NO_LISTING)
        print(f"   Output prices: Steam={steam_ok}, Skinport={skinport_filled}, CSFloat={csfloat_ok}")
        print(f"   Total: {has_price} with price, {no_listing} confirmed no listing.")
    else:
        print(f"   All output prices already in cache.")

    for coll_name, rarities in viable_collections.items():
        if coll_name not in coll_skins:
            print(f"   [SKIP] {coll_name}: not in skin database")
            continue

        for in_rarity, inputs in rarities.items():
            out_rarity = in_rarity + 1
            outputs = coll_skins[coll_name].get(out_rarity, [])
            tag = f"{coll_name} | {rarity_label(in_rarity)}->{rarity_label(out_rarity)}"

            if not outputs:
                skipped_no_outputs += 1
                print(f"   [SKIP] {tag}: no output skins at rarity {out_rarity}")
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
                skipped_stattrak += 1
                print(f"   [SKIP] {tag}: mixed StatTrak split (normal={len(normal_inputs)}, ST={len(stattrak_inputs)}, need 10 of either)")
                continue

            # Sort by best price, take 10 cheapest
            selected_inputs.sort(key=lambda x: x.get("_best_price", 999999))
            top10 = selected_inputs[:10]

            # LIQUIDITY CHECK: Verify we have exactly 10 unique listings
            if len(selected_inputs) < 10:
                skipped_supply += 1
                print(f"   [SKIP] {tag}: insufficient supply ({len(selected_inputs)} listings, need 10)")
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

            # Look up prices for outputs: Steam first, Skinport fallback, CSFloat last resort
            ev_sum = 0
            out_info = []
            has_price = False

            # Fee per price source:
            #   Steam: price is what buyers pay (includes 15% Steam fee). Seller receives price * 0.85
            #   Skinport: seller fee ~8%. Seller receives price * 0.92
            #   CSFloat: seller fee 2%. Seller receives price * 0.98
            SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}

            for name, cond, fv, out_min, out_max in output_conditions:
                cache_key = f"{name}|{cond}"

                if cache_key in cached_prices:
                    cached_val = cached_prices[cache_key]
                    prices_from_cache += 1
                    if cached_val == CSFLOAT_NO_LISTING:
                        price = 0
                        price_source = "NO_LISTINGS"
                    elif cached_val > 0:
                        price = cached_val
                        price_source = price_sources.get(cache_key, "Steam")
                    else:
                        price = 0
                        price_source = "none"
                else:
                    # Not pre-fetched — try Steam on the fly
                    trend = fetch_steam_trend(name, cond)
                    time.sleep(0.35)
                    if trend:
                        steam_ref = trend.get("median") or trend.get("lowest")
                        if steam_ref and steam_ref > 0:
                            price = steam_ref
                            price_source = "Steam"
                            cached_prices[cache_key] = price
                            price_sources[cache_key] = "Steam"
                            prices_fetched += 1
                        else:
                            price = 0
                            price_source = "none"
                    else:
                        price = 0
                        price_source = "none"

                # Apply the correct fee for the price source
                fee = SOURCE_FEES.get(price_source, 0.15)  # Default to 15% if unknown
                price_after_fee = int(price * (1 - fee)) if price else 0

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
                    "price_source": price_source,
                    "probability": prob,
                })

            if not has_price:
                skipped_no_price += 1
                print(f"   [SKIP] {tag}: no price on CSFloat or Skinport for any output ({[o['name'] for o in out_info]})")
                continue

            # Check if any output has unknown price — makes EV unverifiable
            missing_price_outputs = [o["name"] for o in out_info if o["price_raw"] <= 0]
            has_missing_prices = len(missing_price_outputs) > 0
            if has_missing_prices:
                print(f"   [WARN] {tag}: {len(missing_price_outputs)}/{len(out_info)} outputs have no price — EV unverifiable")

            # Skip if all outputs are WW or BS (no value in those conditions)
            ww_bs_only = all(o["condition"] in ("Well-Worn", "Battle-Scarred") for o in out_info)
            if ww_bs_only:
                skipped_not_mw += 1
                out_summary = ", ".join(f"{o['name']} -> {o['condition']} (fv={o['float']:.4f})" for o in out_info)
                print(f"   [SKIP] {tag}: outputs all WW/BS: {out_summary}")
                continue

            # Determine the best output condition achieved
            condition_priority = {"Factory New": 3, "Minimal Wear": 2, "Field-Tested": 1}
            best_cond = max(
                (o["condition"] for o in out_info if o["condition"] in condition_priority),
                key=lambda c: condition_priority.get(c, 0),
                default="Field-Tested"
            )

            # Calculate EV
            net_ev = ev_sum - input_cost
            roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

            # Calculate max allowed float for each input skin type
            # Use the FT threshold (0.38) since that's what Phase 1 actually filtered by.
            # The actual output condition depends on the output skin's float range,
            # not a single global threshold.
            filter_target = 0.38  # FT ceiling — matches Phase 1 filter
            min_max_adjusted = None
            for out in outputs:
                max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"], filter_target)
                if max_adj is not None:
                    if min_max_adjusted is None or max_adj < min_max_adjusted:
                        min_max_adjusted = max_adj

            # Build inputs with max_raw_float calculated
            inputs_with_limits = []
            float_violations = 0
            for x in top10:
                extra = x.get("extra", {})
                skin_min = extra.get("skin_min", 0)
                skin_max = extra.get("skin_max", 1)
                skin_range = skin_max - skin_min
                raw_float = extra.get("floatValue", 0)
                adjusted_float = (raw_float - skin_min) / skin_range if skin_range > 0 else 0
                max_raw = calc_max_input_float_for_skin(min_max_adjusted, skin_min, skin_max) if min_max_adjusted else None
                # Hard assertion: input float must not exceed max_raw_float
                if max_raw is not None and raw_float > max_raw + 0.0001:  # tiny epsilon for float rounding
                    float_violations += 1
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

            if float_violations > 0:
                print(f"   [BUG] {tag}: {float_violations}/10 inputs have float > max_raw_float!")

            results.append({
                "collection": coll_name,
                "in_rarity": in_rarity,
                "out_rarity": out_rarity,
                "target_condition": best_cond,
                "input_cost": input_cost,
                "avg_float": avg_float,
                "max_adjusted": min_max_adjusted,
                "ev_output": ev_sum,
                "ev": net_ev,
                "roi": roi,
                "is_stattrak": is_stattrak_tradeup,
                "outputs": out_info,
                "inputs": inputs_with_limits,
                "unverifiable": has_missing_prices,
            })

    print(f"   Prices: {prices_fetched} fetched, {prices_from_cache} from cache")
    print(f"   Calculated {len(results)} trade-ups")
    total_skipped = skipped_no_outputs + skipped_stattrak + skipped_supply + skipped_no_price + skipped_not_mw
    if total_skipped:
        print(f"   Skipped {total_skipped} total:")
        if skipped_no_outputs:  print(f"     {skipped_no_outputs} - no output skins at next rarity")
        if skipped_stattrak:    print(f"     {skipped_stattrak} - mixed StatTrak (can't split evenly)")
        if skipped_supply:      print(f"     {skipped_supply} - insufficient supply (<10 listings)")
        if skipped_no_price:    print(f"     {skipped_no_price} - no CSFloat price for outputs")
        if skipped_not_mw:      print(f"     {skipped_not_mw} - output floats all WW/BS (no value)")

    return results, cached_prices, price_sources


def calculate_watchlist_estimates(watchlist_collections, coll_skins, cached_prices, price_sources=None):
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
                if price == CSFLOAT_NO_LISTING:
                    price = 0
                wl_source = (price_sources or {}).get(cache_key, "Steam")
                wl_fee = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}.get(wl_source, 0.15)
                price_after_fee = int(price * (1 - wl_fee)) if price > 0 else 0

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


def verify_dmarket_listing(item_id):
    """Check if a DMarket listing is still active."""
    try:
        r = requests.get(f"https://api.dmarket.com/exchange/v1/market/items/{item_id}",
                        timeout=10)
        if r.ok:
            data = r.json()
            return data.get("status") == "active" and data.get("inMarket", False)
        if r.status_code == 404:
            return False
    except Exception:
        pass
    return None  # Unknown (network error)


def verify_profitable_inputs(profitable_results):
    """Verify that DMarket listings in profitable trade-ups are still active.

    Re-fetches current cheapest listings for skins where listings are sold.
    Recalculates profitability and drops trade-ups that are no longer viable.
    """
    print("\n" + "=" * 70)
    print("VERIFYING INPUT LISTINGS (checking if still available)")
    print("=" * 70)

    if not profitable_results:
        return profitable_results

    verified_results = []
    for result in profitable_results:
        coll = result["collection"]
        sold_count = 0
        verified_count = 0
        unknown_count = 0

        for inp in result["inputs"]:
            source = inp.get("source", "")
            listing_id = inp.get("listing_id", "")

            if source == "DMarket" and listing_id:
                is_active = verify_dmarket_listing(listing_id)
                if is_active is True:
                    inp["_verified"] = True
                    verified_count += 1
                elif is_active is False:
                    inp["_verified"] = False
                    inp["_sold"] = True
                    sold_count += 1
                else:
                    inp["_verified"] = None
                    unknown_count += 1
                time.sleep(0.05)  # Light rate limiting
            elif source == "CSFloat" and listing_id:
                # Already have verify_csfloat_listing for this
                result_check = verify_csfloat_listing(listing_id)
                if result_check["available"]:
                    inp["_verified"] = True
                    verified_count += 1
                else:
                    inp["_verified"] = False
                    inp["_sold"] = True
                    sold_count += 1
                time.sleep(0.2)
            else:
                # Skinport-priced items can't be verified individually
                inp["_verified"] = None
                unknown_count += 1

        if sold_count > 0:
            print(f"   [{coll}] {sold_count} SOLD, {verified_count} active, {unknown_count} unverified — re-fetching...")

            # Re-fetch current listings for this skin to find replacements
            # Get the skin name from the first input
            skin_titles = set()
            for inp in result["inputs"]:
                base = inp["title"]
                for c in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
                    base = base.replace(c, "").strip()
                skin_titles.add(base)

            # Fetch fresh listings for each skin
            fresh_listings = []
            for skin_name in skin_titles:
                items = fetch_skin_raw(skin_name, max_items=50)
                fresh_listings.extend(items)

            if not fresh_listings:
                print(f"   [{coll}] Could not re-fetch listings, dropping trade-up")
                continue

            # Rebuild the inputs: keep verified ones, replace sold ones from fresh listings
            active_inputs = [inp for inp in result["inputs"] if inp.get("_verified") is not False]
            needed = 10 - len(active_inputs)

            if needed > 0:
                # Get listing IDs we already have to avoid duplicates
                used_ids = set(inp.get("listing_id", "") for inp in active_inputs)

                # Process fresh listings through the same float filter
                for item in sorted(fresh_listings, key=lambda x: x.get("price_usd", 999999)):
                    if needed <= 0:
                        break
                    lid = item.get("listing_id", "")
                    if lid in used_ids:
                        continue
                    # Check float is within max allowed
                    max_float = result["inputs"][0].get("max_float", 0.38) if result["inputs"] else 0.38
                    if item.get("float", 1.0) > max_float:
                        continue
                    active_inputs.append({
                        "title": item.get("title", ""),
                        "price": item.get("price_usd", 0),
                        "source": "DMarket",
                        "float": item.get("float", 0),
                        "listing_id": lid,
                        "max_float": max_float,
                        "skin_min": result["inputs"][0].get("skin_min", 0),
                        "skin_max": result["inputs"][0].get("skin_max", 1),
                        "_verified": True,
                        "_fresh": True,
                    })
                    used_ids.add(lid)
                    needed -= 1

            if len(active_inputs) < 10:
                print(f"   [{coll}] Only {len(active_inputs)} active listings found (need 10), dropping")
                continue

            # Recalculate cost with fresh inputs
            active_inputs.sort(key=lambda x: x.get("price", x.get("_best_price", 999999)))
            top10 = active_inputs[:10]
            new_cost = sum(inp.get("price", inp.get("_best_price", 0)) for inp in top10)
            new_ev = result["ev_output"] - new_cost
            new_roi = (new_ev / new_cost * 100) if new_cost > 0 else 0

            if new_roi < 25.0 or new_ev < 30:
                print(f"   [{coll}] No longer profitable after re-price (ROI: {new_roi:.1f}%, EV: ${new_ev/100:.2f}), dropping")
                continue

            fresh_count = sum(1 for inp in top10 if inp.get("_fresh"))
            result["inputs"] = top10
            result["input_cost"] = new_cost
            result["ev"] = new_ev
            result["roi"] = new_roi
            print(f"   [{coll}] Refreshed: {fresh_count} new listings, ROI: {new_roi:.1f}%, EV: ${new_ev/100:.2f}")
        else:
            print(f"   [{coll}] All {verified_count} listings verified active")

        verified_results.append(result)

    print(f"\n   Verified: {len(verified_results)}/{len(profitable_results)} trade-ups still viable")
    return verified_results


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
    print("CS2 TRADE-UP EV CALCULATOR v3.5")
    print("Inputs: DMarket+CSFloat+Waxpeer+Skinport | Outputs: Steam+Skinport (free)")
    print("CSFloat budget: inputs only (40 pages/rarity) | Reserve: 10")
    print("=" * 70)

    # Load skin database
    print("\nLoading CS2 skin database...")
    try:
        r = requests.get(SKINS_URL, timeout=30)
        r.raise_for_status()
        skins_data = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"FATAL: Failed to load skin database from GitHub: {e}")
        return

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
    viable_collections, watchlist_collections = phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges, coll_skins)

    if not viable_collections and not watchlist_collections:
        print("\nNo viable or watchlist collections found")
        return

    # PHASE 2: Calculate EVs
    results, cached_prices, price_sources = phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges)
    save_cache(cached_prices)

    # Sort by ROI, filter for 25%+ ROI and $0.30+ net profit
    MIN_ROI = 25.0
    MIN_EV = 30  # $0.30 in cents
    results.sort(key=lambda x: x["roi"], reverse=True)
    profitable = [r for r in results if r["ev"] > 0 and r["roi"] >= MIN_ROI and r["ev"] >= MIN_EV and not r.get("unverifiable")]
    unverifiable = [r for r in results if r["ev"] > 0 and r["roi"] >= MIN_ROI and r["ev"] >= MIN_EV and r.get("unverifiable")]

    # Verify input listings are still available before showing
    profitable = verify_profitable_inputs(profitable)

    # Re-sort and re-filter after verification (prices may have changed)
    profitable.sort(key=lambda x: x["roi"], reverse=True)
    profitable = [r for r in profitable if r["ev"] > 0 and r["roi"] >= MIN_ROI and r["ev"] >= MIN_EV and not r.get("unverifiable")]

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
    unverifiable_note = f", {len(unverifiable)} excluded (missing output prices)" if unverifiable else ""
    print(f"RESULTS: {len(results)} trade-ups analyzed, {len(all_profitable)} profitable, {len(meets_roi)} with ROI >= {MIN_ROI}%, {len(profitable)} with EV >= ${MIN_EV/100:.2f}{unverifiable_note}")
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
            max_adj_val = r.get('max_adjusted', 0)
            print(f"  Max Adjusted Allowed: {max_adj_val:.4f} (for FT ceiling)" if max_adj_val else "  Max Adjusted Allowed: N/A")
            print(f"  Expected Output Value: ${r['ev_output']/100:.2f} (after platform fees)")
            print(f"  Net Profit (EV): ${r['ev']/100:.2f}")

            # Count output conditions
            cond_counts = {}
            for o in r["outputs"]:
                cond_counts[o["condition"]] = cond_counts.get(o["condition"], 0) + 1
            cond_str = ", ".join(f"{v} {k}" for k, v in sorted(cond_counts.items(), key=lambda x: {"Factory New": 0, "Minimal Wear": 1, "Field-Tested": 2}.get(x[0], 9)))
            print(f"  Output Conditions: {cond_str}")

            print("\n  " + "-" * 76)
            print("  POSSIBLE OUTPUTS:")
            print("  " + "-" * 76)
            for out in r["outputs"]:
                print(f"\n    [{out['probability']*100:.0f}% chance] {out['name']}")
                print(f"    Condition: {out['condition']} (expected output float: {out['float']:.4f})")
                print(f"    Skin Float Range: {out['float_min']:.2f} - {out['float_max']:.2f}")

                if out['price_raw'] > 0:
                    source = out.get('price_source', 'CSFloat')
                    fee_pct = {"Steam": 15, "Skinport": 8, "CSFloat": 2}.get(source, 15)
                    print(f"    Price [{source} -{fee_pct}%]: ${out['price_raw']/100:.2f} (${out['price_after_fee']/100:.2f} after {fee_pct}% fee)")
                    ev_contribution = out['price_after_fee'] * out['probability']
                    print(f"    EV Contribution: ${ev_contribution/100:.2f} ({out['probability']*100:.0f}% × ${out['price_after_fee']/100:.2f})")
                    search_name = out['name'].replace(' ', '%20').replace('|', '%7C')
                    print(f"    Sell on CSFloat: https://csfloat.com/search?market_hash_name={search_name}%20%28{out['condition'].replace(' ', '%20')}%29&sort_by=lowest_price")
                else:
                    print(f"    Price: No listing found on CSFloat or Skinport")

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
                # Strip any condition suffix to get the base skin name for grouping
                base_name = inp["title"]
                for _c in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
                    base_name = base_name.replace(_c, "").strip()
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
                condition = get_condition(avg_flt)

                print(f"\n  {skin_name}:")
                print(f"    Buy: {data['count']}x {condition} (max float: {max_flt:.4f})")

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
                skin_short = inp["title"]
                for _c in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
                    skin_short = skin_short.replace(_c, "").strip()
                skin_short = skin_short[:25]
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
            _dmarket_exterior_map = {
                "Factory New": "factory-new", "Minimal Wear": "minimal-wear",
                "Field-Tested": "field-tested", "Well-Worn": "well-worn",
                "Battle-Scarred": "battle-scarred",
            }
            skin_listings = {}
            for inp in r["inputs"]:
                base_name = inp["title"]
                for _c in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
                    base_name = base_name.replace(_c, "").strip()
                original_source = inp.get("original_source", inp.get("source", "?"))
                listing_id = inp.get("listing_id", "")

                if base_name not in skin_listings:
                    skin_listings[base_name] = {
                        "count": 0,
                        "listings": [],  # List of (source, listing_id, price, float)
                        "market_hash": inp["title"],
                        "max_float": inp.get("max_float", 0.25),
                        "condition": get_condition(inp["float"]),
                    }
                skin_listings[base_name]["count"] += 1

                # Track unique listings (by listing_id) — all 10
                if listing_id and listing_id not in [l[1] for l in skin_listings[base_name]["listings"]]:
                    skin_listings[base_name]["listings"].append(
                        (original_source, listing_id, inp["price"], inp["float"])
                    )

            for skin_name, data in skin_listings.items():
                print(f"\n  {skin_name} ({data['count']}x needed):")

                # Show ALL direct listing links
                if data["listings"]:
                    print(f"    Direct listings from DMarket/CSFloat:")
                    for src, lid, price, flt in data["listings"]:
                        if src == "CSFloat":
                            print(f"      ${price/100:.2f} @ {flt:.4f}: https://csfloat.com/item/{lid}")
                        elif src == "DMarket":
                            print(f"      ${price/100:.2f} @ {flt:.4f}: https://dmarket.com/ingame-items/item-list/csgo-skins?userOfferId={lid}")

                # Show search links with correct condition/exterior
                condition = data["condition"]
                exterior = _dmarket_exterior_map.get(condition, "field-tested")
                market_hash = data["market_hash"].replace(' ', '%20').replace('|', '%7C')
                max_flt = data["max_float"]
                print(f"    Search (max float {max_flt:.4f}, condition: {condition}):")
                print(f"      Skinport: https://skinport.com/market/730?search={market_hash}&sort=price&order=asc")
                print(f"      CSFloat:  https://csfloat.com/search?market_hash_name={market_hash}&max_float={max_flt:.4f}&sort_by=lowest_price&type=buy_now")
                print(f"      DMarket:  https://dmarket.com/ingame-items/item-list/csgo-skins?title={market_hash.replace('%20', '+').replace('%7C', '%257C')}&exterior={exterior}")


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

    # Show unverifiable trade-ups (would be profitable but missing output prices)
    if unverifiable:
        print("\n" + "=" * 70)
        print(f"UNVERIFIABLE EV: {len(unverifiable)} trade-ups excluded (missing output prices)")
        print("=" * 70)
        for r in unverifiable[:10]:
            missing = [o["name"] for o in r["outputs"] if o["price_raw"] <= 0]
            st_tag = " [STATTRAK]" if r.get("is_stattrak") else ""
            print(f"\n  [{r['collection'].upper()}]{st_tag} +{r['roi']:.1f}% ROI | EV: ${r['ev']/100:.2f} (UNRELIABLE)")
            print(f"  {RARITY_NAMES[r['in_rarity']]} -> {RARITY_NAMES[r['out_rarity']]} | Input: ${r['input_cost']/100:.2f}")
            print(f"  Missing prices for: {', '.join(missing)}")
        print()

    # WATCH LIST: Collections with 5-9 inputs (close to executable)
    if watchlist_collections:
        watchlist_results = calculate_watchlist_estimates(watchlist_collections, coll_skins, cached_prices, price_sources)

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
