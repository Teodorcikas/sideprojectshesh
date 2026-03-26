import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import sys
import json
import os
import time
import threading
import unicodedata


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


class MultiKeyRateLimiter:
    """Round-robin rate limiter across multiple CSFloat API keys.

    Each key gets its own RateLimiter (2 req/s) and cooldown tracking.
    On 429, the offending key is cooled down for 60s and the next key is used.
    """
    def __init__(self, keys, max_per_second_per_key=2):
        self.keys = list(keys)
        self.num_keys = len(self.keys)
        self.limiters = [RateLimiter(max_per_second_per_key) for _ in self.keys]
        self._cooldowns = [0.0] * self.num_keys  # timestamp when cooldown expires
        self._index = 0
        self._lock = threading.Lock()

    def acquire(self):
        """Get the next available API key (round-robin, skipping cooled-down keys).

        Returns: (api_key, key_index)
        Raises RuntimeError if all keys are cooling down.
        """
        with self._lock:
            now = time.time()
            # Try each key starting from current index
            for attempt in range(self.num_keys):
                idx = (self._index + attempt) % self.num_keys
                if self._cooldowns[idx] <= now:
                    self._index = (idx + 1) % self.num_keys
                    # Release lock before waiting on rate limiter
                    limiter = self.limiters[idx]
                    key = self.keys[idx]
                    break
            else:
                # All keys cooling down — find the one that recovers soonest
                soonest_idx = min(range(self.num_keys), key=lambda i: self._cooldowns[i])
                wait_time = self._cooldowns[soonest_idx] - now
                limiter = self.limiters[soonest_idx]
                key = self.keys[soonest_idx]
                idx = soonest_idx
                self._index = (soonest_idx + 1) % self.num_keys
                if wait_time > 0:
                    # Sleep outside lock below
                    self._lock.release()
                    time.sleep(wait_time)
                    self._lock.acquire()

        limiter.wait()
        return key, idx

    def report_429(self, key_index, cooldown_seconds=60):
        """Mark a key as cooling down after receiving a 429."""
        with self._lock:
            self._cooldowns[key_index] = time.time() + cooldown_seconds

    def key_count(self):
        return self.num_keys


# Parse CSFloat API keys: CSFLOAT_API_KEYS (comma-separated) takes priority over single CSFLOAT_API_KEY
_csfloat_keys_str = os.environ.get("CSFLOAT_API_KEYS", "")
if _csfloat_keys_str:
    _csfloat_all_keys = [k.strip() for k in _csfloat_keys_str.split(",") if k.strip()]
else:
    _csfloat_all_keys = [
        os.environ.get("CSFLOAT_API_KEY", "skYpZbif0-zYaiAA1nxlQmwL1AsGAZrN"),
        "rWNZgzybNLkx14stW8Aao1QlKu81DGoS",
    ]

_csfloat_multi_limiter = MultiKeyRateLimiter(_csfloat_all_keys, max_per_second_per_key=2)

# Global Steam rate limit tracking
_steam_consecutive_429s = 0
_steam_blocked = False

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

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

DMARKET_URL = "https://api.dmarket.com/exchange/v1/market/items"
SKINPORT_URL = "https://api.skinport.com/v1/items"
CSFLOAT_URL = "https://csfloat.com/api/v1/listings"
WAXPEER_URL = "https://api.waxpeer.com/v1/get-items-list"
WAXPEER_API_KEY = os.environ.get("WAXPEER_API_KEY", "410978b36bea0578ce29902b70e51fc973f4afa063bfb7c211c9ae603b074f1b")
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
# Dynamic CSFloat budget based on number of API keys
# Each key has ~200 request budget per reset period
_CSFLOAT_TOTAL_BUDGET = len(_csfloat_all_keys) * 200
CSFLOAT_BUDGET_RESERVE = max(10, int(_CSFLOAT_TOTAL_BUDGET * 0.05))
CSFLOAT_INPUT_CAP = max(140, int(_CSFLOAT_TOTAL_BUDGET * 0.50))
CSFLOAT_OUTPUT_RESERVE = max(50, int(_CSFLOAT_TOTAL_BUDGET * 0.45))


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
    """Load output price cache. Supports v1 (global timestamp) and v2 (per-key timestamps).
    Returns (prices_dict, timestamps_dict, sources_dict).
    Expired entries (>6h) are dropped on load. Stale entries (3-6h) are kept as fallback.
    """
    if not os.path.exists(CACHE_FILE):
        return {}, {}, {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        now = time.time()

        if data.get("version") == 2:
            # v2: per-key timestamps
            entries = data.get("entries", {})
            prices, sources, timestamps, volumes = {}, {}, {}, {}
            for key, entry in entries.items():
                fetched_at = entry.get("fetched_at", 0)
                if now - fetched_at > CACHE_STALE_EXPIRY:
                    continue  # expired, drop
                prices[key] = entry.get("price", 0)
                sources[key] = entry.get("source", "")
                timestamps[key] = fetched_at
                if "volume_24h" in entry:
                    volumes[key] = entry["volume_24h"]
            return prices, timestamps, sources, volumes
        else:
            # v1: migrate global timestamp to per-key
            ts = data.get("timestamp", 0)
            if now - ts > CACHE_STALE_EXPIRY:
                return {}, {}, {}, {}  # all expired
            prices = data.get("prices", {})
            sources = data.get("sources", {})
            timestamps = {key: ts for key in prices}
            return prices, timestamps, sources, {}
    except (IOError, json.JSONDecodeError, ValueError):
        return {}, {}, {}, {}


def save_cache(prices, sources=None, timestamps=None, volumes=None):
    """Save output price cache in v2 per-key format."""
    now = time.time()
    sources = sources or {}
    timestamps = timestamps or {}
    volumes = volumes or {}
    entries = {}
    for key, price in prices.items():
        entry = {
            "price": price,
            "source": sources.get(key, ""),
            "fetched_at": timestamps.get(key, now),
        }
        if key in volumes:
            entry["volume_24h"] = volumes[key]
        entries[key] = entry
    data = {"version": 2, "entries": entries}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def liquidity_multiplier(volume_24h):
    """Discount factor based on trading volume. High-volume = reliable price, low-volume = risky."""
    if volume_24h is None:
        return 0.85  # Unknown volume — slight discount
    if volume_24h >= 100:
        return 1.0
    if volume_24h >= 10:
        return 0.90
    if volume_24h >= 2:
        return 0.70
    return 0.50  # <2/day = very illiquid


def is_entry_fresh(fetched_at):
    """Check if a cache entry is fresh (< 3h old)."""
    return (time.time() - fetched_at) < CACHE_EXPIRY


def is_entry_usable(fetched_at):
    """Check if a cache entry is usable (< 6h old, fresh or stale)."""
    return (time.time() - fetched_at) < CACHE_STALE_EXPIRY


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
        api_key, key_idx = _csfloat_multi_limiter.acquire()
        headers = {"Authorization": api_key}
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


def verify_opportunity(opportunity, cached_prices, cache_volumes=None):
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
    SOURCE_FEES_VERIFY = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
    ev_sum = 0
    for out in outputs:
        cache_key = f"{out['name']}|{out['condition']}"
        price = cached_prices.get(cache_key, out.get("price_raw", 0))
        # Use the correct fee for this output's price source
        price_source = out.get("price_source", "Steam")
        fee = SOURCE_FEES_VERIFY.get(price_source, 0.15)
        price_after_fee = int(price * (1 - fee)) if price else 0
        # Apply liquidity discount
        if cache_volumes and price_after_fee > 0:
            liq = liquidity_multiplier(cache_volumes.get(cache_key))
            price_after_fee = int(price_after_fee * liq)
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


def verify_saved_opportunities(cached_prices, cache_volumes=None):
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
        status, updated = verify_opportunity(opp, cached_prices, cache_volumes)
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
    """Append profitable trade-ups to the persistent winners.md log (with dedup)."""
    if not profitable_results:
        return

    from datetime import date
    today = date.today().isoformat()

    # Build set of already-logged entries for today to avoid duplicates across runs
    existing_keys = set()
    if os.path.exists(WINNERS_FILE):
        try:
            with open(WINNERS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            # Parse "### Collection | Rarity → Rarity | ..." lines under "## YYYY-MM-DD" sections
            current_date = None
            for line in content.split("\n"):
                if line.startswith("## ") and not line.startswith("### "):
                    current_date = line[3:].strip()
                elif line.startswith("### ") and current_date == today:
                    # Extract collection and rarity from "### Collection | Rarity → Rarity | ..."
                    parts = line[4:].split("|")
                    if len(parts) >= 2:
                        key = f"{parts[0].strip()}|{parts[1].strip()}"
                        existing_keys.add(key)
        except (IOError, UnicodeDecodeError):
            pass

    new_results = []
    for r in profitable_results:
        coll = r["collection"].title()
        in_r = RARITY_NAMES[r["in_rarity"]].title()
        out_r = RARITY_NAMES[r["out_rarity"]].title()
        dedup_key = f"{coll}|{in_r} → {out_r}"
        if dedup_key not in existing_keys:
            new_results.append(r)

    if not new_results:
        print(f"   Winners log: {len(profitable_results)} already logged today, skipping")
        return

    lines = [f"\n## {today}\n"]
    for r in new_results:
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

    skipped = len(profitable_results) - len(new_results)
    msg = f"   Appended {len(new_results)} winner(s) to winners.md"
    if skipped:
        msg += f" ({skipped} already logged today)"
    print(msg)


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
        # max_adjusted is normalized 0-1. To get a safe raw float upper bound for
        # server-side filtering, we need to account for the widest possible skin range.
        # Worst case: skin with range 0.0-1.0 → max_raw = max_adjusted * 1.0.
        # But many skins have narrower ranges (e.g., 0.0-0.5) where max_raw would be lower.
        # Use the adjusted value directly as raw bound (covers the worst case 0-1 range),
        # capped at 0.45 (WW ceiling) to be safe — client-side filter refines per-skin.
        raw_upper = min(max_adjusted, 0.45)
        if in_rarity not in rarity_max_floats or raw_upper > rarity_max_floats[in_rarity]:
            rarity_max_floats[in_rarity] = raw_upper

    if not rarity_max_floats:
        print("   [CSFLOAT] No rarities to fetch")
        return []

    all_items = []
    seen_listing_ids = set()
    total_requests = 0

    for rarity_int, max_float in sorted(rarity_max_floats.items()):
        rarity_name = RARITY_NAMES[rarity_int] if rarity_int < len(RARITY_NAMES) else f"rarity_{rarity_int}"
        csfloat_rarity = RARITY_TO_CSFLOAT.get(rarity_name, rarity_int + 1)

        # Check budget before starting this rarity
        # Stop if: hit input cap OR remaining budget would eat into output reserve
        input_floor = CSFLOAT_OUTPUT_RESERVE + CSFLOAT_BUDGET_RESERVE
        with _csfloat_budget["lock"]:
            remaining = _csfloat_budget["remaining"]
        if total_requests >= CSFLOAT_INPUT_CAP:
            print(f"   [CSFLOAT] Input cap hit ({total_requests}/{CSFLOAT_INPUT_CAP} requests used), stopping input fetch")
            break
        if remaining is not None and remaining <= input_floor:
            print(f"   [CSFLOAT] Budget low ({remaining} left, reserving {input_floor} for outputs+testing), stopping input fetch")
            break

        min_price = 0
        rarity_items = 0

        for page in range(max_pages_per_rarity):
            # Check budget EVERY page, not just per rarity
            if total_requests >= CSFLOAT_INPUT_CAP:
                print(f"   [CSFLOAT] Input cap hit ({total_requests}/{CSFLOAT_INPUT_CAP}), stopping {rarity_name}")
                break
            with _csfloat_budget["lock"]:
                remaining = _csfloat_budget["remaining"]
            if remaining is not None and remaining <= input_floor:
                print(f"   [CSFLOAT] Budget floor hit ({remaining} left, reserving {input_floor}), stopping {rarity_name}")
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
                api_key, key_idx = _csfloat_multi_limiter.acquire()
                headers = {"Authorization": api_key}
                r = requests.get(CSFLOAT_URL, params=params, headers=headers, timeout=15)
                _update_csfloat_budget(r)
                total_requests += 1

                if r.status_code == 429:
                    _csfloat_multi_limiter.report_429(key_idx)
                    with _csfloat_budget["lock"]:
                        rl_reset = _csfloat_budget["reset"]
                    wait_time = 10
                    if rl_reset:
                        wait_time = max(1, rl_reset - int(time.time()) + 1)
                        if wait_time > 300:
                            print(f"   [CSFLOAT 429] {rarity_name} p{page+1} key#{key_idx} | reset in {wait_time}s (>5m, stopping)")
                            break
                    print(f"   [CSFLOAT 429] {rarity_name} p{page+1} key#{key_idx} | waiting {wait_time}s")
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
                    "source": "DMarket",
                    "listing_id": item_id,
                })

            cursor = data.get("cursor")
            if not cursor:
                break
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"   WARNING: DMarket fetch interrupted for '{skin_name}' ({e}), got {len(all_items)} so far")
            break

    return all_items


def fetch_waxpeer_listings(skin_names, skin_db_lookup, max_pages=200):
    """Fetch Waxpeer listings via bulk pagination (sorted by price).

    Uses bulk fetch instead of per-skin search — much faster.
    100 items/page, ~10-60% have float values depending on price range.
    skin_db_lookup: dict of skin_name -> list of {collection, quality} from skin database.
    Returns items in the same normalized format as DMarket/CSFloat.
    """
    if not WAXPEER_API_KEY:
        print("   [WAXPEER] No API key set — skipping (set WAXPEER_API_KEY)")
        return []

    all_items = []
    seen_ids = set()
    total_requests = 0
    skipped_no_float = 0
    skipped_no_db = 0

    for page in range(max_pages):
        skip = page * 100
        params = {
            "api": WAXPEER_API_KEY,
            "game": "csgo",
            "order_by": "price",
            "order": "ASC",
            "skip": skip,
            "min_price": 1,  # Skip near-zero graffiti/stickers
        }
        try:
            r = requests.get(WAXPEER_URL, params=params, timeout=15)
            total_requests += 1

            if r.status_code == 429:
                print(f"   [WAXPEER] Rate limited at page {page}, stopping")
                break
            if not r.ok:
                print(f"   [WAXPEER] HTTP {r.status_code} at page {page}, stopping")
                break

            data = r.json()
            if not data.get("success"):
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                item_name = item.get("name", "")
                price_raw = item.get("price", 0)  # Waxpeer uses 1$ = 1000 (millicents)
                price = round(price_raw / 10)  # Convert to cents (100 = $1.00) to match other sources
                fv = item.get("float")
                item_id = item.get("item_id", "")

                if not fv or price <= 0:
                    skipped_no_float += 1
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
                    skipped_no_db += 1
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

            if len(items) < 100:
                break  # Last page

            time.sleep(0.3)  # Respect rate limit

            # Progress every 50 pages
            if (page + 1) % 50 == 0:
                print(f"   [WAXPEER] {page+1} pages, {len(all_items)} items with float so far...")

        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            print(f"   [WAXPEER] Fetch error at page {page}: {e}")
            break

    print(f"   [WAXPEER] Fetched {len(all_items)} items with floats, {total_requests} pages "
          f"({skipped_no_float} no float, {skipped_no_db} not in skin DB)")
    return all_items


def extract_skin_name(title):
    """Extract base skin name from title (e.g., 'AK-47 | Redline (Field-Tested)' -> 'AK-47 | Redline')."""
    # Normalize Unicode first (e.g., StatTrak™ variants)
    title = unicodedata.normalize("NFKC", title)
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
            float_violations += 1
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
            "_best_source": "Skinport" if price_from_skinport else item_source,
            "_original_source": item_source,  # Where the listing/float came from
            "_price_from_skinport": price_from_skinport,
            "_is_stattrak": is_stattrak,
            "_listing_id": item.get("listing_id", ""),
        })

    if float_violations > 0:
        print(f"   [FILTER] {float_violations} items rejected — float > max_raw_float")
    return processed


def phase0_reverse_search(coll_skins, cached_prices, price_sources, cache_volumes=None):
    """Reverse search: rank output skins by value, work backwards to identify priority collections.

    Uses ONLY cached data — zero API calls. Returns set of collection names worth prioritizing
    in Phase 1 for budget allocation.
    """
    if cache_volumes is None:
        cache_volumes = {}
    if not cached_prices:
        return set()

    print("\n" + "=" * 70)
    print("PHASE 0: Reverse search (top-down from valuable outputs)")
    print("=" * 70)

    SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}

    # Build index of valuable outputs: (net_price, skin_name, condition, collection, input_rarity)
    valuable_outputs = []
    for coll_name, rarities in coll_skins.items():
        for out_rarity, skins in rarities.items():
            in_rarity = out_rarity - 1
            if in_rarity < 0:
                continue
            for skin in skins:
                for cond in ["Factory New", "Minimal Wear", "Field-Tested"]:
                    cache_key = f"{skin['name']}|{cond}"
                    price = cached_prices.get(cache_key, 0)
                    if price and price > 0:
                        source = price_sources.get(cache_key, "Steam")
                        fee = SOURCE_FEES.get(source, 0.15)
                        liq = liquidity_multiplier(cache_volumes.get(cache_key))
                        net_price = int(price * (1 - fee) * liq)
                        if net_price >= 500:  # $5+ outputs only
                            valuable_outputs.append({
                                "net_price": net_price,
                                "name": skin["name"],
                                "condition": cond,
                                "collection": coll_name,
                                "in_rarity": in_rarity,
                                "out_rarity": out_rarity,
                                "n_outputs": len(skins),
                                "source": source,
                            })
                        break  # Only count best condition per skin

    valuable_outputs.sort(key=lambda x: x["net_price"], reverse=True)

    if not valuable_outputs:
        print("   No valuable outputs ($5+) found in cache")
        return set()

    # For each valuable output, calculate break-even input cost
    # EV from one output = net_price * (1/n_outputs)   (for a single-collection trade-up)
    # Break-even: 10 * max_input_price = total_EV * (1 / (1 + MIN_ROI/100))
    # For 25% ROI: max_total_input = total_EV / 1.25
    priority_collections = set()
    candidates = []

    for out in valuable_outputs[:50]:  # Top 50 most valuable
        # Estimate total EV from this collection (sum all outputs)
        total_ev = 0
        for skin in coll_skins.get(out["collection"], {}).get(out["out_rarity"], []):
            for cond in ["Factory New", "Minimal Wear", "Field-Tested"]:
                cache_key = f"{skin['name']}|{cond}"
                price = cached_prices.get(cache_key, 0)
                if price and price > 0:
                    source = price_sources.get(cache_key, "Steam")
                    fee = SOURCE_FEES.get(source, 0.15)
                    liq = liquidity_multiplier(cache_volumes.get(cache_key))
                    total_ev += int(price * (1 - fee) * liq) / out["n_outputs"]
                    break

        if total_ev <= 0:
            continue

        # Break-even input cost for 25% ROI
        max_total_input = total_ev / 1.25
        max_per_input = max_total_input / 10

        candidates.append({
            "collection": out["collection"],
            "best_output": f"{out['name']} ({out['condition']}) ${out['net_price']/100:.2f}",
            "total_ev": total_ev,
            "max_per_input": max_per_input,
            "in_rarity": out["in_rarity"],
        })
        priority_collections.add(out["collection"])

    # Deduplicate candidates by collection
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c["collection"] not in seen:
            seen.add(c["collection"])
            unique_candidates.append(c)

    print(f"   Found {len(valuable_outputs)} valuable outputs ($5+) across {len(priority_collections)} collections")
    for c in unique_candidates[:10]:
        print(f"   [{c['collection']}] {c['best_output']} — max input ${c['max_per_input']/100:.2f}/ea for 25% ROI")

    return priority_collections


def _classify_collections(all_items):
    """Group processed items by collection+rarity, deduplicate, split into viable/watchlist.

    Returns: (viable, watchlist, by_collection)
      viable: dict of collections with 10+ inputs per rarity
      watchlist: dict of collections with 5-9 inputs per rarity
      by_collection: full grouped dict (all counts)
    """
    by_collection = defaultdict(lambda: defaultdict(list))
    seen_by_coll_rarity = defaultdict(set)
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

        if not listing_id:
            fv = extra.get("floatValue", 0)
            price = item.get("_best_price", 0)
            title = item.get("title", "")
            listing_id = f"_synth_{title}_{price}_{fv}"

        key = (coll, RARITY_ORDER[quality])
        if listing_id in seen_by_coll_rarity[key]:
            duplicates_skipped += 1
            continue
        seen_by_coll_rarity[key].add(listing_id)

        by_collection[coll][RARITY_ORDER[quality]].append(item)

    if duplicates_skipped > 0:
        print(f"   Duplicates skipped: {duplicates_skipped}")

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

    return viable, watchlist, by_collection


def waxpeer_targeted_fetch(watchlist_collections, coll_skins, skin_db_lookup, float_limits):
    """Fetch Waxpeer listings for specific skins in near-viable (watchlist) collections.

    For each watchlist collection+rarity, queries Waxpeer's search endpoint by exact skin name.
    This targeted approach often yields items with float data that the bulk fetch missed.

    Returns: list of items in the same format as fetch_waxpeer_listings().
    """
    if not WAXPEER_API_KEY:
        return []

    skins_to_fetch = set()
    for coll_name, rarities in watchlist_collections.items():
        for rarity_int in rarities:
            # Get the input rarity skins for this collection
            in_skins = coll_skins.get(coll_name, {}).get(rarity_int, [])
            for skin in in_skins:
                skins_to_fetch.add(skin["name"])

    if not skins_to_fetch:
        return []

    print(f"   [WAXPEER TARGETED] Searching {len(skins_to_fetch)} skins from {len(watchlist_collections)} near-viable collections...")

    all_items = []
    fetched = 0
    for skin_name in skins_to_fetch:
        try:
            params = {
                "api": WAXPEER_API_KEY,
                "game": "csgo",
                "search": skin_name,
                "minified": 1,
            }
            r = requests.get(WAXPEER_URL, params=params, timeout=15)
            fetched += 1

            if not r.ok:
                continue

            data = r.json()
            items = data.get("items", [])

            for it in items:
                fv = it.get("float")
                if fv is None or fv <= 0:
                    continue

                item_name = it.get("name", "")
                item_id = str(it.get("item_id", ""))
                price = it.get("price", 0)
                if price > 0:
                    price = price // 10  # Waxpeer prices are in millicents

                if "Souvenir" in item_name:
                    continue

                base_name = extract_skin_name(item_name)
                db_entries = skin_db_lookup.get(base_name, [])
                if not db_entries:
                    continue

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

            time.sleep(0.3)

        except (requests.RequestException, json.JSONDecodeError, ValueError):
            continue

    print(f"   [WAXPEER TARGETED] Found {len(all_items)} items with floats from {fetched} queries")
    return all_items


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

    # Build skin_db_lookup (needed by Waxpeer bulk + targeted fetch)
    skin_db_lookup = defaultdict(list)
    for coll_name, rarities in coll_skins.items():
        for rarity_int, skins in rarities.items():
            quality = RARITY_NAMES[rarity_int] if rarity_int < len(RARITY_NAMES) else str(rarity_int)
            for skin in skins:
                skin_db_lookup[skin["name"]].append({
                    "collection": coll_name,
                    "quality": quality,
                })

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

        # Fetch same skin set as DMarket
        skins_to_fetch = set()
        for (coll, in_rarity) in float_limits:
            for skin in coll_skins.get(coll, {}).get(in_rarity, []):
                skins_to_fetch.add(skin["name"])

        print(f"   [WAXPEER] Bulk fetching listings (200 pages max, ~2 min)...")
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

    # Classify into viable/watchlist using shared helper
    viable, watchlist, by_collection = _classify_collections(all_items)

    print(f"   Viable collections (10+ inputs): {len(viable)}")
    print(f"   Watchlist collections (5-9 inputs): {len(watchlist)}")
    return viable, watchlist, by_collection, all_items, skin_db_lookup


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
    """Look up price from pre-fetched Skinport data.

    Returns dict with 'price' and 'quantity' keys, or None if not found.
    """
    # Normalize Unicode for consistent StatTrak™ lookup across platforms
    market_hash_name = unicodedata.normalize("NFKC", f"{skin_name} ({condition})")
    price = skinport_prices.get(market_hash_name)
    if price is None:
        # Try without normalization as fallback (in case Skinport keys aren't normalized)
        price = skinport_prices.get(f"{skin_name} ({condition})")
    if price is None:
        return None
    return price


CSFLOAT_NO_LISTING = -1  # Sentinel: CSFloat confirmed zero active listings for this skin

def fetch_csfloat_price(skin_name, condition):
    """Fetch lowest CSFloat buy-now price for a skin+condition.

    Returns:
      price > 0          — CSFloat has an active listing at this price (cents)
      CSFLOAT_NO_LISTING — CSFloat responded 200 OK but zero listings; skin not sellable here
      0                  — Rate-limited after all retries, or network error; do NOT cache

    Uses the global MultiKeyRateLimiter for key rotation and rate limiting.
    Retries on 429 with key rotation and exponential backoff.
    """
    if not _csfloat_has_budget():
        return 0  # Budget exhausted — don't waste remaining reserve
    market_hash_name = f"{skin_name} ({condition})"
    params = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": 1,
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
                        return 0
                else:
                    wait_time = min(5 * (2 ** attempt), 60)
                print(f"   [CSFLOAT 429] attempt {attempt+1}/{max_attempts} key#{key_idx} | {rl_remaining}/{rl_limit} remaining | waiting {wait_time}s")
                time.sleep(wait_time)
                continue
            if r.status_code in (401, 403):
                print(f"   [CSFLOAT] Auth error {r.status_code} key#{key_idx} for '{market_hash_name}' — check API key")
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


def phase2_calculate_ev(viable_collections, coll_skins, cached_prices, skinport_prices, skin_float_ranges, cached_sources=None, cache_timestamps=None, cache_volumes=None):
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
    if cache_timestamps is None:
        cache_timestamps = {}
    if cache_volumes is None:
        cache_volumes = {}

    # --- PRE-FETCH: Collect all output (name, cond) pairs and bulk-fetch in parallel ---
    # Show CSFloat budget status from Phase 1
    with _csfloat_budget["lock"]:
        remaining = _csfloat_budget["remaining"]
        limit = _csfloat_budget["limit"]
    if remaining is not None:
        print(f"\n   [CSFLOAT BUDGET] {remaining}/{limit} requests remaining ({CSFLOAT_OUTPUT_RESERVE} reserved for outputs, {CSFLOAT_BUDGET_RESERVE} reserve)")
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
                if cache_key not in cached_prices or not is_entry_fresh(cache_timestamps.get(cache_key, 0)):
                    all_output_pairs.add((out["name"], out_cond))

    # Track price sources: cache_key -> "Steam" / "Skinport" / "CSFloat"
    # Restore sources from cache so cached prices keep their correct source label
    price_sources = dict(cached_sources) if cached_sources else {}

    if all_output_pairs:
        # OUTPUT PRICING STRATEGY:
        # CSFloat budget is spent on INPUTS (where exact float matters).
        # For outputs, condition is what matters — use free sources:
        #   1. Skinport (free, already fetched in bulk) — sell fee 8%
        #   2. CSFloat if budget allows — sell fee 2%
        #   3. Steam on-the-fly as last resort only (slow, 1.5s/req) — sell fee 15%

        # STEP 1: Skinport prices (free, already fetched in bulk)
        # First pass: collect Skinport candidates that pass singleton filter
        skinport_candidates = []  # (name, cond, cache_key, sp_data)
        skinport_rejected_singleton = 0
        still_missing = []
        for name, cond in all_output_pairs:
            cache_key = f"{name}|{cond}"
            if cached_prices.get(cache_key, 0) > 0:
                continue
            sp_data = get_skinport_price(skinport_prices, name, cond)
            if sp_data is None or sp_data.get("price", 0) <= 0:
                still_missing.append((name, cond))
                continue
            if sp_data.get("quantity", 0) < 2:
                skinport_rejected_singleton += 1
                still_missing.append((name, cond))
                continue
            skinport_candidates.append((name, cond, cache_key, sp_data))

        # Build DMarket reference prices from Phase 1 cache (already on disk, no API calls)
        # DMarket titles are "Skin Name (Condition)" — build cheapest price per title
        dmarket_refs = {}
        dmarket_items_raw, _, dmarket_status = load_dmarket_cache()
        if dmarket_items_raw:
            for item in dmarket_items_raw:
                title = item.get("title", "")
                price = item.get("price_usd", 0)
                if title and price > 0:
                    if title not in dmarket_refs or price < dmarket_refs[title]:
                        dmarket_refs[title] = price
            print(f"   DMarket reference prices: {len(dmarket_refs)} skins loaded for sanity check")

        # Steam refs as fallback (from cache only — no fetching)
        steam_refs = {}
        for key, val in cached_prices.items():
            if val > 0 and price_sources.get(key) == "Steam":
                steam_refs[key] = val

        # Fetch Steam ONLY for candidates with no DMarket ref
        needs_steam = []
        for name, cond, cache_key, sp_data in skinport_candidates:
            dm_title = f"{name} ({cond})"
            if dm_title not in dmarket_refs and cache_key not in steam_refs:
                needs_steam.append((name, cond, cache_key))
        if needs_steam:
            print(f"   Fetching {len(needs_steam)} Steam reference prices (no DMarket data)...")
            for i, (name, cond, cache_key) in enumerate(needs_steam):
                if (i + 1) % 25 == 0 or i == 0:
                    print(f"   [STEAM] Progress: {i+1}/{len(needs_steam)}...")
                if _steam_blocked:
                    break
                trend = fetch_steam_trend(name, cond)
                if trend:
                    steam_ref = trend.get("median") or trend.get("lowest")
                    if steam_ref and steam_ref > 0:
                        steam_refs[cache_key] = steam_ref
            print(f"   Steam refs fetched: {len(steam_refs)} available")
            if _steam_blocked:
                print(f"   [STEAM] IP blocked — Skinport sanity check using DMarket refs only, CSFloat verification for output prices")

        # Second pass: apply 2× sanity check — DMarket first, Steam fallback
        skinport_filled = 0
        skinport_rejected_inflated = 0
        for name, cond, cache_key, sp_data in skinport_candidates:
            sp_price = sp_data["price"]
            dm_title = f"{name} ({cond})"
            dm_ref = dmarket_refs.get(dm_title)
            steam_ref = steam_refs.get(cache_key)

            # Check DMarket first (2× threshold)
            if dm_ref and sp_price > dm_ref * 2:
                print(f"   [REJECTED] {name} ({cond}): Skinport ${sp_price/100:.2f} vs DMarket ${dm_ref/100:.2f} ({sp_price/dm_ref:.1f}x)")
                skinport_rejected_inflated += 1
                still_missing.append((name, cond))
                continue
            # Fallback: check Steam (2× threshold)
            if not dm_ref and steam_ref and sp_price > steam_ref * 2:
                print(f"   [REJECTED] {name} ({cond}): Skinport ${sp_price/100:.2f} vs Steam ${steam_ref/100:.2f} ({sp_price/steam_ref:.1f}x)")
                skinport_rejected_inflated += 1
                still_missing.append((name, cond))
                continue
            # No reference at all — warn but accept
            if not dm_ref and not steam_ref:
                print(f"   [WARN] {name} ({cond}): Skinport ${sp_price/100:.2f} — no DMarket/Steam ref to validate")
            cached_prices[cache_key] = sp_price
            price_sources[cache_key] = "Skinport"
            cache_timestamps[cache_key] = time.time()
            skinport_filled += 1
        if skinport_filled:
            print(f"   Skinport: {skinport_filled} filled")
        if skinport_rejected_singleton:
            print(f"   Skinport rejected: {skinport_rejected_singleton} singletons (qty=1)")
        if skinport_rejected_inflated:
            print(f"   Skinport rejected: {skinport_rejected_inflated} inflated (>2× DMarket/Steam)")

        # STEP 2: CSFloat — verify/replace Skinport prices for promising trade-ups
        # CSFloat is a larger marketplace with more reliable prices and lower sell fee (2% vs 8%)
        csfloat_ok = 0
        csfloat_replaced = 0
        if _csfloat_has_budget():
            # Pre-scan: estimate EV per trade-up, rank by ROI, collect outputs for verification
            SOURCE_FEES_PRESCAN = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
            tradeup_outputs = []  # (roi_estimate, [(name, cond), ...])

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
                    input_cost = sum(x.get("_best_price", 0) for x in top10)
                    if input_cost <= 0:
                        continue

                    input_data = []
                    for x in top10:
                        extra = x.get("extra", {})
                        input_data.append({
                            "float": extra.get("floatValue", 0.18),
                            "skin_min": extra.get("skin_min", 0.0),
                            "skin_max": extra.get("skin_max", 1.0),
                        })

                    ev_sum = 0
                    output_pairs = []
                    for out in outputs:
                        out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
                        out_cond = get_condition(out_fv)
                        cache_key = f"{out['name']}|{out_cond}"
                        prob = 1 / len(outputs)
                        price = cached_prices.get(cache_key, 0)
                        if price > 0:
                            fee = SOURCE_FEES_PRESCAN.get(price_sources.get(cache_key, "Steam"), 0.15)
                            liq = liquidity_multiplier(cache_volumes.get(cache_key))
                            ev_sum += int(price * (1 - fee) * liq) * prob
                        # Only queue non-WW/BS outputs for CSFloat verification (WW/BS rarely worth the budget)
                        if out_cond not in ("Well-Worn", "Battle-Scarred") and price_sources.get(cache_key) != "CSFloat":
                            output_pairs.append((out["name"], out_cond))

                    if output_pairs:
                        roi = ((ev_sum / input_cost) - 1) * 100 if input_cost > 0 else -100
                        tradeup_outputs.append((roi, output_pairs))

            # Sort by ROI descending — verify most promising trade-ups first
            tradeup_outputs.sort(key=lambda x: x[0], reverse=True)

            # Collect output pairs prioritized by trade-up ROI, up to budget
            with _csfloat_budget["lock"]:
                remaining = _csfloat_budget["remaining"]
            available_budget = max(0, (remaining or 0) - CSFLOAT_BUDGET_RESERVE)

            to_fetch = []
            seen = set()
            for roi, pairs in tradeup_outputs:
                for pair in pairs:
                    if pair not in seen and len(to_fetch) < available_budget:
                        to_fetch.append(pair)
                        seen.add(pair)
            total_candidates = len(seen) + sum(1 for _, pairs in tradeup_outputs for p in pairs if p not in seen)

            if to_fetch:
                print(f"   CSFloat verification: {len(to_fetch)} outputs ({remaining} budget, {available_budget} usable)")

                def _fetch_output_price(name_cond):
                    name, cond = name_cond
                    return name_cond, fetch_csfloat_price(name, cond)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(_fetch_output_price, pair): pair for pair in to_fetch}
                    for future in as_completed(futures):
                        (name, cond), price = future.result()
                        cache_key = f"{name}|{cond}"
                        if price > 0:
                            had_price = cache_key in cached_prices and cached_prices[cache_key] > 0
                            cached_prices[cache_key] = price
                            price_sources[cache_key] = "CSFloat"
                            cache_timestamps[cache_key] = time.time()
                            csfloat_ok += 1
                            if had_price:
                                csfloat_replaced += 1
                        elif price == CSFLOAT_NO_LISTING:
                            # Keep Skinport price if available — can still sell on Skinport
                            if cache_key not in cached_prices or cached_prices[cache_key] <= 0:
                                cached_prices[cache_key] = CSFLOAT_NO_LISTING
                                cache_timestamps[cache_key] = time.time()
                print(f"   CSFloat: {csfloat_ok} prices ({csfloat_replaced} replaced Skinport)")
        else:
            print(f"   CSFloat budget exhausted — Skinport prices only (unverified)")

        has_price = sum(1 for v in cached_prices.values() if v > 0)
        no_listing = sum(1 for v in cached_prices.values() if v == CSFLOAT_NO_LISTING)
        print(f"   Output prices: Skinport={skinport_filled}, CSFloat={csfloat_ok}")
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
                        # CSFloat has no listing — try Skinport as sell platform (qty >= 2 guard)
                        sp_data = get_skinport_price(skinport_prices, name, cond)
                        if sp_data and sp_data.get("price", 0) > 0 and sp_data.get("quantity", 0) >= 2:
                            price = sp_data["price"]
                            price_source = "Skinport"
                            cached_prices[cache_key] = price
                            price_sources[cache_key] = "Skinport"
                            cache_timestamps[cache_key] = time.time()
                        else:
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
                    if trend:
                        steam_ref = trend.get("median") or trend.get("lowest")
                        if steam_ref and steam_ref > 0:
                            price = steam_ref
                            price_source = "Steam"
                            cached_prices[cache_key] = price
                            price_sources[cache_key] = "Steam"
                            cache_timestamps[cache_key] = time.time()
                            if trend.get("volume_24h") is not None:
                                cache_volumes[cache_key] = trend["volume_24h"]
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

                # Apply liquidity discount based on Steam trading volume
                volume = cache_volumes.get(cache_key)
                liq_mult = liquidity_multiplier(volume)
                price_after_fee = int(price_after_fee * liq_mult)

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
                    "volume_24h": volume,
                    "liquidity_mult": liq_mult,
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
                    "original_source": x.get("_original_source", x.get("_best_source", "?")),
                    "price_from_skinport": x.get("_price_from_skinport", False),
                    "float": raw_float,
                    "adjusted_float": adjusted_float,
                    "skin_min": skin_min,
                    "skin_max": skin_max,
                    "max_float": max_raw,
                    "listing_id": x.get("_listing_id", ""),
                })

            if float_violations > 0:
                print(f"   [ERROR] {tag}: {float_violations}/10 inputs have float > max_raw_float — SKIPPING")
                continue

            # Flag if no output price was verified on CSFloat (all from Steam/Skinport)
            has_csfloat_price = any(o.get("price_source") == "CSFloat" for o in out_info if o["price_raw"] > 0)

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
                "unverified_csfloat": not has_csfloat_price,
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

    return results, cached_prices, price_sources, cache_timestamps, cache_volumes


# ============ TWO-PASS EV SYSTEM ============


def _get_best_free_price(skin_name, condition, cached_prices, cached_sources, skinport_prices,
                         cache_timestamps=None, cache_volumes=None, relaxed=False):
    """Get the best available price for an output skin WITHOUT making API calls.

    Sources checked (in order): cache (any source) > Skinport bulk > nothing.
    If relaxed=True: allows Skinport singletons (flagged), warns instead of rejecting >2x.

    Returns: (price, source, confidence) where confidence is 'high', 'medium', or 'low'
    """
    cache_key = f"{skin_name}|{condition}"

    # 1. Check cache (any source — may be hours old but still valid)
    cached_val = cached_prices.get(cache_key, 0)
    if cached_val and cached_val > 0:
        source = cached_sources.get(cache_key, "Steam")
        return cached_val, source, "high"
    if cached_val == CSFLOAT_NO_LISTING:
        # Confirmed no CSFloat listing — try Skinport as sell platform
        sp_data = get_skinport_price(skinport_prices, skin_name, condition)
        if sp_data and sp_data.get("price", 0) > 0:
            qty = sp_data.get("quantity", 0)
            if qty >= 2 or relaxed:
                conf = "medium" if qty >= 2 else "low"
                return sp_data["price"], "Skinport", conf
        return 0, "NO_LISTINGS", "none"

    # 2. Skinport bulk prices
    sp_data = get_skinport_price(skinport_prices, skin_name, condition)
    if sp_data and sp_data.get("price", 0) > 0:
        qty = sp_data.get("quantity", 0)
        if qty >= 2:
            return sp_data["price"], "Skinport", "medium"
        elif relaxed:
            return sp_data["price"], "Skinport", "low"

    return 0, "none", "none"


def _evaluate_tradeup(inputs_10, coll_skins, out_rarity, cached_prices, cached_sources,
                      skinport_prices, cache_timestamps=None, cache_volumes=None,
                      apply_liquidity=False, relaxed_filters=False, coll_counts=None):
    """Core EV calculation for a trade-up (single or multi-collection).

    Args:
        inputs_10: list of 10 input item dicts (from process_cached_items)
        coll_skins: skin database
        out_rarity: output rarity int
        coll_counts: dict of {collection: input_count} for multi-collection, or None for single
        apply_liquidity: whether to apply liquidity discount
        relaxed_filters: if True, relax Skinport filters (Pass 1 mode)

    Returns: dict with ev, roi, outputs, input_cost, etc. or None if not evaluable.
    """
    SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}

    # Determine collection counts
    if coll_counts is None:
        # Single collection — all inputs from same collection
        extra0 = inputs_10[0].get("extra", {})
        colls0 = extra0.get("collection", [])
        if not colls0:
            return None
        coll_name = colls0[0].lower().replace("the ", "").replace(" collection", "").strip()
        coll_counts = {coll_name: 10}

    # Build input data for float calculation
    input_data = []
    for x in inputs_10:
        extra = x.get("extra", {})
        input_data.append({
            "float": extra.get("floatValue", 0.18),
            "skin_min": extra.get("skin_min", 0.0),
            "skin_max": extra.get("skin_max", 1.0),
        })

    input_cost = sum(x.get("_best_price", 0) for x in inputs_10)
    if input_cost <= 0:
        return None

    avg_float = sum(d["float"] for d in input_data) / 10

    # Calculate EV across all contributing collections
    ev_sum = 0
    out_info = []
    has_price = False
    has_missing = False
    all_wwbs = True

    for coll, n_inputs in coll_counts.items():
        outputs = coll_skins.get(coll, {}).get(out_rarity, [])
        if not outputs:
            continue
        for out in outputs:
            out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
            out_cond = get_condition(out_fv)

            if out_cond not in ("Well-Worn", "Battle-Scarred"):
                all_wwbs = False

            prob = (n_inputs / 10) * (1 / len(outputs))

            price, price_source, confidence = _get_best_free_price(
                out["name"], out_cond, cached_prices, cached_sources, skinport_prices,
                cache_timestamps, cache_volumes, relaxed=relaxed_filters
            )

            fee = SOURCE_FEES.get(price_source, 0.15)
            price_after_fee = int(price * (1 - fee)) if price > 0 else 0

            if apply_liquidity and price_after_fee > 0:
                cache_key = f"{out['name']}|{out_cond}"
                volume = (cache_volumes or {}).get(cache_key)
                liq_mult = liquidity_multiplier(volume)
                price_after_fee = int(price_after_fee * liq_mult)
            else:
                cache_key = f"{out['name']}|{out_cond}"
                volume = (cache_volumes or {}).get(cache_key)
                liq_mult = 1.0

            if price_after_fee > 0:
                has_price = True
            elif confidence == "none" and price_source != "NO_LISTINGS":
                has_missing = True

            ev_sum += price_after_fee * prob
            out_info.append({
                "name": out["name"],
                "condition": out_cond,
                "float": out_fv,
                "float_min": out["min_float"],
                "float_max": out["max_float"],
                "price_raw": price if price > 0 else 0,
                "price_after_fee": price_after_fee,
                "price_source": price_source,
                "probability": prob,
                "from_collection": coll if len(coll_counts) > 1 else None,
                "volume_24h": volume,
                "liquidity_mult": liq_mult,
                "confidence": confidence,
            })

    if not has_price:
        return None

    if all_wwbs:
        return None

    net_ev = ev_sum - input_cost
    roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

    return {
        "ev": net_ev,
        "roi": roi,
        "ev_output": ev_sum,
        "input_cost": input_cost,
        "avg_float": avg_float,
        "outputs": out_info,
        "unverifiable": has_missing,
        "coll_counts": dict(coll_counts),
    }


def phase2_pass1_broad_scan(viable_collections, all_inputs_by_collection, coll_skins,
                            cached_prices, cached_sources, skinport_prices, skin_float_ranges,
                            cache_timestamps=None, cache_volumes=None):
    """Pass 1: Broad scan using only free/cached data. Zero API calls.

    Evaluates ALL single-collection AND multi-collection trade-ups with relaxed filters.
    Returns ranked candidates + set of unique output skins needing verification.
    """
    print("\n" + "=" * 70)
    print("PHASE 2 PASS 1: Broad Scan (free data only, relaxed filters)")
    print("=" * 70)

    SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
    MAX_COLLECTIONS = 4
    candidates = []
    outputs_to_verify = set()  # (name, condition) pairs

    # --- SINGLE-COLLECTION TRADE-UPS ---
    single_count = 0
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

            for selected, is_st in [(normal_inputs, False), (stattrak_inputs, True)]:
                if len(selected) < 10:
                    continue
                selected.sort(key=lambda x: x.get("_best_price", 999999))
                top10 = selected[:10]

                result = _evaluate_tradeup(
                    top10, coll_skins, out_rarity, cached_prices, cached_sources,
                    skinport_prices, cache_timestamps, cache_volumes,
                    apply_liquidity=False, relaxed_filters=True,
                    coll_counts={coll_name: 10}
                )
                if result is None:
                    continue

                single_count += 1
                result["collection"] = coll_name
                result["in_rarity"] = in_rarity
                result["out_rarity"] = out_rarity
                result["is_stattrak"] = is_st
                result["multi_collection"] = False
                result["inputs_raw"] = top10
                candidates.append(result)

                # Collect outputs needing verification
                for o in result["outputs"]:
                    if o["price_raw"] > 0 and o["confidence"] != "high":
                        outputs_to_verify.add((o["name"], o["condition"]))
                    elif o["price_raw"] <= 0 and o["price_source"] != "NO_LISTINGS":
                        outputs_to_verify.add((o["name"], o["condition"]))

    print(f"   Single-collection candidates: {single_count}")

    # --- MULTI-COLLECTION TRADE-UPS ---
    # Build global input pools by rarity (non-StatTrak)
    global_pools = defaultdict(list)
    for coll_name, rarities in all_inputs_by_collection.items():
        for rarity, inputs in rarities.items():
            for inp in inputs:
                if not inp.get("_is_stattrak", False):
                    global_pools[rarity].append((inp, coll_name))
    for rarity in global_pools:
        global_pools[rarity].sort(key=lambda x: x[0].get("_best_price", 999999))

    colls_with_outputs = defaultdict(set)
    for coll_name in coll_skins:
        for rarity in coll_skins[coll_name]:
            if coll_skins[coll_name][rarity]:
                colls_with_outputs[rarity].add(coll_name)

    # Pre-compute ev_per_input for filler scoring
    ev_per_input = {}
    max_single_output = {}
    for coll_name in coll_skins:
        for out_rarity_key in coll_skins[coll_name]:
            outputs = coll_skins[coll_name][out_rarity_key]
            if not outputs:
                continue
            total_val = 0
            best_single = 0
            for out in outputs:
                for try_cond in ["Minimal Wear", "Field-Tested", "Factory New"]:
                    cache_key = f"{out['name']}|{try_cond}"
                    price = cached_prices.get(cache_key, 0)
                    if price and price > 0:
                        source = cached_sources.get(cache_key, "Steam")
                        fee = SOURCE_FEES.get(source, 0.15)
                        paf = int(price * (1 - fee))
                        total_val += paf
                        if paf > best_single:
                            best_single = paf
                        break
            n_outputs = len(outputs)
            ev_per_input[(coll_name, out_rarity_key)] = total_val / (10 * n_outputs) if n_outputs > 0 else 0
            max_single_output[(coll_name, out_rarity_key)] = best_single

    # Pre-compute max adjusted float limits
    coll_max_adjusted = {}
    for coll_name in coll_skins:
        for out_rarity_key in coll_skins[coll_name]:
            outputs = coll_skins[coll_name][out_rarity_key]
            min_max_adj = None
            for out in outputs:
                max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"], 0.38)
                if max_adj is not None:
                    if min_max_adj is None or max_adj < min_max_adj:
                        min_max_adj = max_adj
            if min_max_adj is not None:
                coll_max_adjusted[(coll_name, out_rarity_key)] = min_max_adj

    multi_count = 0
    seen_tradeups = set()

    for in_rarity in sorted(global_pools.keys()):
        out_rarity = in_rarity + 1
        pool = global_pools[in_rarity]
        if len(pool) < 10:
            continue

        coll_inputs = defaultdict(list)
        for inp, coll_name in pool:
            max_adj_limit = coll_max_adjusted.get((coll_name, out_rarity))
            if max_adj_limit is not None:
                extra = inp.get("extra", {})
                skin_min = extra.get("skin_min", 0)
                skin_max = extra.get("skin_max", 1)
                max_raw = calc_max_input_float_for_skin(max_adj_limit, skin_min, skin_max)
                raw_float = extra.get("floatValue", 0)
                if max_raw is not None and raw_float > max_raw + 0.0001:
                    continue
            coll_inputs[coll_name].append(inp)

        for target_coll in colls_with_outputs.get(out_rarity, set()):
            target_normal = coll_inputs.get(target_coll, [])
            if len(target_normal) < 1:
                continue

            target_ev_per_slot = ev_per_input.get((target_coll, out_rarity), 0)
            if target_ev_per_slot <= 0:
                continue

            max_from_target = min(len(target_normal), 9)
            for n_target in range(1, max_from_target + 1):
                needed = 10 - n_target
                if needed <= 0:
                    continue

                target_selected = target_normal[:n_target]
                target_cost = sum(x.get("_best_price", 0) for x in target_selected)
                target_listing_ids = {x.get("_listing_id", "") for x in target_selected}

                target_ev_contribution = target_ev_per_slot * n_target
                if target_ev_contribution < target_cost * 0.5 and n_target >= 5:
                    continue

                # Cheapest filler strategy only for Pass 1 (speed)
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
                    filler_by_coll[inp_coll].append(inp)

                if not filler_by_coll:
                    continue

                # Strategy A only (cheapest) for broad scan speed
                def _avg_cost(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    return -(sum(f.get("_best_price", 999999) for f in f_inputs[:n]) / n) if n > 0 else 0
                fillers = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _avg_cost)
                if len(fillers) < needed:
                    continue

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

    print(f"   Multi-collection candidates: {multi_count}")

    # Rank by optimistic ROI descending
    candidates.sort(key=lambda x: x["roi"], reverse=True)

    # Filter to promising candidates (any positive ROI for Pass 1)
    promising = [c for c in candidates if c["roi"] > 0]

    print(f"   Total candidates: {len(candidates)} ({len(promising)} with positive ROI)")
    print(f"   Unique output skins to verify: {len(outputs_to_verify)}")

    return promising, outputs_to_verify


def phase2_pass2_deep_verify(candidates, outputs_to_verify, coll_skins,
                             cached_prices, cached_sources, skinport_prices, skin_float_ranges,
                             cache_timestamps=None, cache_volumes=None):
    """Pass 2: Verify output prices for top candidates using CSFloat as primary.

    CSFloat-primary: if CSFloat has no listing, skin is excluded (NO_CSFLOAT_LISTINGS).
    Steam is only used for volume_24h liquidity data.

    Returns: (results, cached_prices, price_sources, cache_timestamps, cache_volumes)
    """
    print("\n" + "=" * 70)
    print("PHASE 2 PASS 2: Deep Verify (CSFloat-primary)")
    print("=" * 70)

    SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
    if cache_timestamps is None:
        cache_timestamps = {}
    if cache_volumes is None:
        cache_volumes = {}
    price_sources = dict(cached_sources) if cached_sources else {}

    # Prioritize outputs by the highest-ROI candidate that needs them
    output_priority = {}  # (name, cond) -> best ROI
    for cand in candidates:
        for o in cand["outputs"]:
            key = (o["name"], o["condition"])
            if key in outputs_to_verify:
                if key not in output_priority or cand["roi"] > output_priority[key]:
                    output_priority[key] = cand["roi"]

    # Sort outputs by priority (highest ROI first)
    prioritized_outputs = sorted(output_priority.keys(), key=lambda k: output_priority[k], reverse=True)

    # Determine budget
    with _csfloat_budget["lock"]:
        remaining = _csfloat_budget["remaining"]
    available_budget = max(0, (remaining or CSFLOAT_OUTPUT_RESERVE) - CSFLOAT_BUDGET_RESERVE)
    # Also cap at CSFLOAT_OUTPUT_RESERVE to not eat into input budget for next run
    budget = min(len(prioritized_outputs), CSFLOAT_OUTPUT_RESERVE, available_budget)

    to_verify = prioritized_outputs[:budget]
    print(f"   Verifying {len(to_verify)}/{len(prioritized_outputs)} output skins (budget: {budget})")

    # Fetch CSFloat prices for outputs
    csfloat_ok = 0
    csfloat_no_listing = 0
    csfloat_failed = 0

    if to_verify:
        def _fetch_output_price(name_cond):
            name, cond = name_cond
            return name_cond, fetch_csfloat_price(name, cond)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(_fetch_output_price, pair): pair for pair in to_verify}
            for future in as_completed(futures):
                (name, cond), price = future.result()
                cache_key = f"{name}|{cond}"
                if price > 0:
                    cached_prices[cache_key] = price
                    price_sources[cache_key] = "CSFloat"
                    cache_timestamps[cache_key] = time.time()
                    csfloat_ok += 1
                elif price == CSFLOAT_NO_LISTING:
                    # Keep Skinport price if available
                    if cache_key not in cached_prices or cached_prices[cache_key] <= 0:
                        cached_prices[cache_key] = CSFLOAT_NO_LISTING
                        cache_timestamps[cache_key] = time.time()
                    csfloat_no_listing += 1
                else:
                    csfloat_failed += 1

        print(f"   CSFloat: {csfloat_ok} priced, {csfloat_no_listing} no listing, {csfloat_failed} failed")

    # Fetch Steam volume data for verified outputs (liquidity only, ~50-100 calls)
    steam_volume_fetched = 0
    verified_output_keys = set()
    for name, cond in to_verify:
        cache_key = f"{name}|{cond}"
        if cached_prices.get(cache_key, 0) > 0:
            verified_output_keys.add(cache_key)

    for cache_key in verified_output_keys:
        if cache_key in cache_volumes:
            continue  # Already have volume data
        parts = cache_key.rsplit("|", 1)
        if len(parts) != 2:
            continue
        name, cond = parts
        if _steam_blocked:
            break
        trend = fetch_steam_trend(name, cond)
        if trend and trend.get("volume_24h") is not None:
            cache_volumes[cache_key] = trend["volume_24h"]
            steam_volume_fetched += 1

    if steam_volume_fetched:
        print(f"   Steam volume data: {steam_volume_fetched} skins fetched")

    # Re-evaluate all candidates with verified prices + strict filters
    print(f"\n   Re-evaluating {len(candidates)} candidates with verified prices...")
    results = []
    rarity_label = lambda r: RARITY_NAMES[r] if r < len(RARITY_NAMES) else str(r)

    for cand in candidates:
        out_rarity = cand["out_rarity"]
        in_rarity = cand["in_rarity"]
        inputs_raw = cand["inputs_raw"]
        coll_counts_orig = cand.get("coll_counts", {})
        is_multi = cand.get("multi_collection", False)

        # Determine coll_counts for single vs multi
        if is_multi:
            coll_counts = coll_counts_orig
        else:
            coll_counts = {cand["collection"]: 10}

        # Re-evaluate with strict filters and liquidity
        result = _evaluate_tradeup(
            inputs_raw, coll_skins, out_rarity, cached_prices, price_sources,
            skinport_prices, cache_timestamps, cache_volumes,
            apply_liquidity=True, relaxed_filters=False,
            coll_counts=coll_counts
        )
        if result is None:
            continue

        # Apply ROI + EV threshold
        MIN_ROI = 25.0
        MIN_EV = 30
        if result["roi"] < MIN_ROI or result["ev"] < MIN_EV:
            continue

        # Build max_adjusted for display
        filter_target = 0.38
        min_max_adjusted = None
        for coll in coll_counts:
            for out in coll_skins.get(coll, {}).get(out_rarity, []):
                max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"], filter_target)
                if max_adj is not None:
                    if min_max_adjusted is None or max_adj < min_max_adjusted:
                        min_max_adjusted = max_adj

        # Build inputs_with_limits
        inputs_with_limits = []
        float_violations = 0
        if is_multi:
            all_10_with_colls = cand.get("inputs_raw_with_colls", [(inp, list(coll_counts.keys())[0]) for inp in inputs_raw])
        else:
            all_10_with_colls = [(inp, cand["collection"]) for inp in inputs_raw]

        for x_item, from_coll in all_10_with_colls:
            # x_item may be a tuple (inp, coll) from multi-collection
            if isinstance(x_item, tuple):
                x, from_coll = x_item
            else:
                x = x_item
            extra = x.get("extra", {})
            skin_min = extra.get("skin_min", 0)
            skin_max = extra.get("skin_max", 1)
            skin_range = skin_max - skin_min
            raw_float = extra.get("floatValue", 0)
            adjusted_float = (raw_float - skin_min) / skin_range if skin_range > 0 else 0
            max_raw = calc_max_input_float_for_skin(min_max_adjusted, skin_min, skin_max) if min_max_adjusted else None
            if max_raw is not None and raw_float > max_raw + 0.0001:
                float_violations += 1
            inp_entry = {
                "title": x.get("title"),
                "price": x.get("_best_price", 0),
                "source": x.get("_best_source", "?"),
                "original_source": x.get("_original_source", x.get("_best_source", "?")),
                "price_from_skinport": x.get("_price_from_skinport", False),
                "float": raw_float,
                "adjusted_float": adjusted_float,
                "skin_min": skin_min,
                "skin_max": skin_max,
                "max_float": max_raw,
                "listing_id": x.get("_listing_id", ""),
            }
            if is_multi:
                inp_entry["from_collection"] = from_coll
            inputs_with_limits.append(inp_entry)

        if float_violations > 0:
            tag = f"{cand['collection']} | {rarity_label(in_rarity)}->{rarity_label(out_rarity)}"
            print(f"   [ERROR] {tag}: {float_violations}/10 inputs have float > max_raw_float — SKIPPING")
            continue

        # Determine best output condition
        condition_priority = {"Factory New": 3, "Minimal Wear": 2, "Field-Tested": 1}
        priced_outputs = [o for o in result["outputs"] if o["price_after_fee"] > 0]
        best_cond = max(
            (o["condition"] for o in priced_outputs if o["condition"] in condition_priority),
            key=lambda c: condition_priority.get(c, 0),
            default="Field-Tested"
        ) if priced_outputs else "Unknown"

        has_csfloat_price = any(o.get("price_source") == "CSFloat" for o in result["outputs"] if o["price_raw"] > 0)
        has_missing_prices = any(o["price_raw"] <= 0 and o["price_source"] != "NO_LISTINGS" for o in result["outputs"])

        final_result = {
            "collection": cand["collection"],
            "in_rarity": in_rarity,
            "out_rarity": out_rarity,
            "target_condition": best_cond,
            "input_cost": result["input_cost"],
            "avg_float": result["avg_float"],
            "max_adjusted": min_max_adjusted,
            "ev_output": result["ev_output"],
            "ev": result["ev"],
            "roi": result["roi"],
            "is_stattrak": cand.get("is_stattrak", False),
            "outputs": result["outputs"],
            "inputs": inputs_with_limits,
            "unverifiable": has_missing_prices,
            "unverified_csfloat": not has_csfloat_price,
            "multi_collection": is_multi,
        }
        if is_multi:
            final_result["target_collection"] = cand.get("target_collection")
            final_result["coll_counts"] = coll_counts
            final_result["coll_mix"] = cand.get("coll_mix", "")

        results.append(final_result)

    results.sort(key=lambda x: x["roi"], reverse=True)
    print(f"   Verified profitable trade-ups: {len(results)}")

    return results, cached_prices, price_sources, cache_timestamps, cache_volumes


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


def _select_fillers(filler_by_coll, needed, used_listing_ids, max_filler_colls, sort_key_fn):
    """Select fillers from ranked collections using the given sort key function.

    Returns list of (input_dict, collection_name) tuples, up to `needed` items.
    """
    ranked = sorted(filler_by_coll.items(), key=sort_key_fn, reverse=True)
    fillers = []
    filler_colls_used = set()
    used_ids = set(used_listing_ids)

    for f_coll, f_inputs in ranked:
        if len(filler_colls_used) >= max_filler_colls:
            break
        if len(fillers) >= needed:
            break
        for f_inp in f_inputs:
            lid = f_inp.get("_listing_id", "")
            if lid in used_ids:
                continue
            fillers.append((f_inp, f_coll))
            filler_colls_used.add(f_coll)
            used_ids.add(lid)
            if len(fillers) >= needed:
                break

    return fillers


# ============ PHASE 2b: MULTI-COLLECTION TRADE-UPS ============

def phase2_multi_collection_ev(all_inputs_by_collection, viable_collections, coll_skins,
                                cached_prices, price_sources, skin_float_ranges, skinport_prices=None, cache_timestamps=None, cache_volumes=None):
    """
    Find profitable trade-ups by mixing inputs from multiple collections (max 4).

    Strategy: for each collection with outputs at the next rarity, try multiple
    split ratios with filler collections. Fillers are scored by a blend of input
    cheapness AND output value (weighted probability × output price). Explores
    hundreds/thousands of combinations instead of just one per target.

    Probability per output = (inputs_from_coll / 10) * (1 / outputs_in_coll)
    """
    print("\n" + "=" * 70)
    print("PHASE 2b: Multi-collection trade-ups (target + fillers, max 4 collections)")
    print("=" * 70)

    SOURCE_FEES = {"Steam": 0.15, "Skinport": 0.08, "CSFloat": 0.02}
    MAX_COLLECTIONS = 4
    MIN_ROI = 25.0
    MIN_EV = 30  # $0.30 in cents
    results = []
    considered = 0
    skipped_no_fillers = 0
    skipped_no_outputs = 0
    skipped_no_price = 0
    skipped_wwbs = 0
    if cache_timestamps is None:
        cache_timestamps = {}
    if cache_volumes is None:
        cache_volumes = {}

    # Build global input pools by rarity (across all collections), non-StatTrak only
    global_pools = defaultdict(list)  # rarity_int -> [(input_dict, collection_name)]
    for coll_name, rarities in all_inputs_by_collection.items():
        for rarity, inputs in rarities.items():
            for inp in inputs:
                if not inp.get("_is_stattrak", False):
                    global_pools[rarity].append((inp, coll_name))

    # Pre-sort each pool by price (cheapest first)
    for rarity in global_pools:
        global_pools[rarity].sort(key=lambda x: x[0].get("_best_price", 999999))

    # Pre-compute which collections have outputs at each rarity
    colls_with_outputs = defaultdict(set)  # rarity -> set of collection names
    for coll_name in coll_skins:
        for rarity in coll_skins[coll_name]:
            if coll_skins[coll_name][rarity]:
                colls_with_outputs[rarity].add(coll_name)

    # ==========================================
    # BUG #1 FIX: Pre-fetch output prices for ALL collections that might participate
    # in multi-collection trade-ups (not just viable_collections).
    # Phase 2 only fetched prices for viable collections. Filler/watchlist collection
    # outputs had no prices in cache, zeroing out their EV contribution.
    # ==========================================
    all_needed_output_pairs = set()  # (name, condition) pairs needing prices
    for in_rarity in sorted(global_pools.keys()):
        out_rarity = in_rarity + 1
        # Collect outputs from ALL collections that have outputs at this rarity
        for coll_name in colls_with_outputs.get(out_rarity, set()):
            outputs = coll_skins.get(coll_name, {}).get(out_rarity, [])
            for out in outputs:
                # Use a representative float estimate for condition determination
                # Use pool average float as approximation (exact float depends on inputs chosen)
                pool = global_pools.get(in_rarity, [])
                if not pool:
                    continue
                # Sample: take avg float of cheapest 10 inputs in pool for condition estimate
                sample = pool[:min(10, len(pool))]
                sample_data = []
                for inp, _ in sample:
                    extra = inp.get("extra", {})
                    sample_data.append({
                        "float": extra.get("floatValue", 0.18),
                        "skin_min": extra.get("skin_min", 0.0),
                        "skin_max": extra.get("skin_max", 1.0),
                    })
                out_fv = calc_output_float(sample_data, out["min_float"], out["max_float"])
                out_cond = get_condition(out_fv)
                cache_key = f"{out['name']}|{out_cond}"
                if cache_key not in cached_prices or not is_entry_fresh(cache_timestamps.get(cache_key, 0)):
                    all_needed_output_pairs.add((out["name"], out_cond))

    prices_fetched_multi = 0
    if all_needed_output_pairs:
        print(f"   [MULTI-COLL] Need prices for {len(all_needed_output_pairs)} output skins not in cache")

        # Step 1: Try Skinport (free, already fetched in bulk)
        skinport_filled = 0
        still_missing_multi = []
        if skinport_prices:
            for name, cond in all_needed_output_pairs:
                cache_key = f"{name}|{cond}"
                if cached_prices.get(cache_key, 0) > 0:
                    continue
                sp_data = get_skinport_price(skinport_prices, name, cond)
                if sp_data is None or sp_data.get("price", 0) <= 0:
                    still_missing_multi.append((name, cond))
                    continue
                if sp_data.get("quantity", 0) < 2:
                    still_missing_multi.append((name, cond))
                    continue
                cached_prices[cache_key] = sp_data["price"]
                price_sources[cache_key] = "Skinport"
                cache_timestamps[cache_key] = time.time()
                skinport_filled += 1
                prices_fetched_multi += 1
            if skinport_filled:
                print(f"   [MULTI-COLL] Skinport: filled {skinport_filled} output prices")
        else:
            still_missing_multi = list(all_needed_output_pairs)

        # Step 2: Try Steam for remaining (on-the-fly, respects rate limits)
        if still_missing_multi:
            steam_filled = 0
            print(f"   [MULTI-COLL] Fetching {len(still_missing_multi)} output prices from Steam...")
            for i, (name, cond) in enumerate(still_missing_multi):
                if (i + 1) % 25 == 0 or i == 0:
                    print(f"   [MULTI-COLL STEAM] Progress: {i+1}/{len(still_missing_multi)}...")
                cache_key = f"{name}|{cond}"
                if cached_prices.get(cache_key, 0) > 0:
                    continue
                trend = fetch_steam_trend(name, cond)
                if trend:
                    steam_ref = trend.get("median") or trend.get("lowest")
                    if steam_ref and steam_ref > 0:
                        cached_prices[cache_key] = steam_ref
                        price_sources[cache_key] = "Steam"
                        cache_timestamps[cache_key] = time.time()
                        steam_filled += 1
                        prices_fetched_multi += 1
                if _steam_blocked:
                    print(f"   [MULTI-COLL] Steam blocked — stopping output price fetch")
                    break
            if steam_filled:
                print(f"   [MULTI-COLL] Steam: filled {steam_filled} output prices")

        # Step 3: Use remaining CSFloat budget to verify/upgrade the most valuable output prices
        # CSFloat has 2% fee vs Skinport 8% / Steam 15%, so verifying here improves net EV
        if _csfloat_has_budget():
            with _csfloat_budget["lock"]:
                remaining = _csfloat_budget["remaining"]
            available = max(0, (remaining or 0) - CSFLOAT_BUDGET_RESERVE)

            if available > 0:
                # Collect outputs that have a price but NOT from CSFloat — prioritize highest value
                csfloat_candidates = []
                for name, cond in all_needed_output_pairs:
                    cache_key = f"{name}|{cond}"
                    price = cached_prices.get(cache_key, 0)
                    source = price_sources.get(cache_key, "")
                    if price > 0 and source != "CSFloat":
                        csfloat_candidates.append((price, name, cond))
                # Also include outputs that were already cached from Phase 2 but not CSFloat-verified
                for key, price in cached_prices.items():
                    if price > 0 and price_sources.get(key, "") != "CSFloat" and "|" in key:
                        parts = key.rsplit("|", 1)
                        if len(parts) == 2:
                            name, cond = parts
                            candidate = (price, name, cond)
                            if candidate not in csfloat_candidates:
                                csfloat_candidates.append(candidate)

                # Sort by price descending — verify most valuable outputs first
                csfloat_candidates.sort(key=lambda x: x[0], reverse=True)
                to_verify = csfloat_candidates[:available]

                if to_verify:
                    print(f"   [MULTI-COLL] CSFloat verification: {len(to_verify)} outputs (budget: {available})")
                    csfloat_ok = 0
                    csfloat_replaced = 0

                    def _fetch_multi_output(item):
                        _, name, cond = item
                        return (name, cond), fetch_csfloat_price(name, cond)

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = {executor.submit(_fetch_multi_output, item): item for item in to_verify}
                        for future in as_completed(futures):
                            (name, cond), price = future.result()
                            cache_key = f"{name}|{cond}"
                            if price > 0:
                                had_price = cache_key in cached_prices and cached_prices[cache_key] > 0
                                cached_prices[cache_key] = price
                                price_sources[cache_key] = "CSFloat"
                                cache_timestamps[cache_key] = time.time()
                                csfloat_ok += 1
                                if had_price:
                                    csfloat_replaced += 1
                            elif price == CSFLOAT_NO_LISTING:
                                if cache_key not in cached_prices or cached_prices[cache_key] <= 0:
                                    cached_prices[cache_key] = CSFLOAT_NO_LISTING
                                    cache_timestamps[cache_key] = time.time()

                    print(f"   [MULTI-COLL] CSFloat: {csfloat_ok} verified ({csfloat_replaced} replaced Skinport/Steam)")
                    prices_fetched_multi += csfloat_ok

        print(f"   [MULTI-COLL] Total new output prices fetched: {prices_fetched_multi}")
    else:
        print(f"   [MULTI-COLL] All needed output prices already in cache")

    # ==========================================
    # BUG #3 FIX: Pre-compute estimated output value per collection+rarity
    # so filler selection considers output value, not just input cheapness.
    # ==========================================
    coll_output_value = {}  # (coll_name, out_rarity) -> estimated total output value (cents)
    for coll_name in coll_skins:
        for out_rarity_key in coll_skins[coll_name]:
            outputs = coll_skins[coll_name][out_rarity_key]
            if not outputs:
                continue
            total_val = 0
            for out in outputs:
                # Try multiple conditions (FN, MW, FT) and use whichever has a price
                for try_cond in ["Minimal Wear", "Field-Tested", "Factory New"]:
                    cache_key = f"{out['name']}|{try_cond}"
                    price = cached_prices.get(cache_key, 0)
                    if price and price > 0:
                        source = price_sources.get(cache_key, "Steam")
                        fee = SOURCE_FEES.get(source, 0.15)
                        liq = liquidity_multiplier(cache_volumes.get(cache_key))
                        total_val += int(price * (1 - fee) * liq)
                        break
            coll_output_value[(coll_name, out_rarity_key)] = total_val

    # Pre-compute per-input EV contribution and max single output price per collection
    # ev_per_input[(coll, rarity)] = (1/10) * sum(price_after_fee) / n_outputs
    # max_single_output[(coll, rarity)] = highest single output price_after_fee
    ev_per_input = {}
    max_single_output = {}
    for coll_name in coll_skins:
        for out_rarity_key in coll_skins[coll_name]:
            outputs = coll_skins[coll_name][out_rarity_key]
            if not outputs:
                continue
            n_outputs = len(outputs)
            total_val = coll_output_value.get((coll_name, out_rarity_key), 0)
            ev_per_input[(coll_name, out_rarity_key)] = total_val / (10 * n_outputs) if n_outputs > 0 else 0
            best_single = 0
            for out in outputs:
                for try_cond in ["Minimal Wear", "Field-Tested", "Factory New"]:
                    cache_key = f"{out['name']}|{try_cond}"
                    price = cached_prices.get(cache_key, 0)
                    if price and price > 0:
                        source = price_sources.get(cache_key, "Steam")
                        fee = SOURCE_FEES.get(source, 0.15)
                        liq = liquidity_multiplier(cache_volumes.get(cache_key))
                        paf = int(price * (1 - fee) * liq)
                        if paf > best_single:
                            best_single = paf
                        break
            max_single_output[(coll_name, out_rarity_key)] = best_single

    # ==========================================
    # BUG #8 FIX: Pre-filter inputs that violate float limits before building pools.
    # Compute max adjusted float per collection+rarity, then filter out violating inputs.
    # This avoids rejecting entire combos due to a single bad input.
    # ==========================================
    # Build per-collection max_adjusted limits for float violation checking
    coll_max_adjusted = {}  # (coll_name, out_rarity) -> min_max_adjusted across all outputs
    for coll_name in coll_skins:
        for out_rarity_key in coll_skins[coll_name]:
            outputs = coll_skins[coll_name][out_rarity_key]
            min_max_adj = None
            for out in outputs:
                max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"], 0.38)
                if max_adj is not None:
                    if min_max_adj is None or max_adj < min_max_adj:
                        min_max_adj = max_adj
            if min_max_adj is not None:
                coll_max_adjusted[(coll_name, out_rarity_key)] = min_max_adj

    # Track seen trade-ups to avoid duplicates
    seen_tradeups = set()

    for in_rarity in sorted(global_pools.keys()):
        out_rarity = in_rarity + 1
        pool = global_pools[in_rarity]
        if len(pool) < 10:
            continue

        # Build per-collection input lists at this rarity, pre-filtered for float violations
        coll_inputs = defaultdict(list)  # coll_name -> [input_dict, ...] sorted by price
        for inp, coll_name in pool:
            # BUG #8: Check float violation BEFORE selection
            max_adj_limit = coll_max_adjusted.get((coll_name, out_rarity))
            if max_adj_limit is not None:
                extra = inp.get("extra", {})
                skin_min = extra.get("skin_min", 0)
                skin_max = extra.get("skin_max", 1)
                max_raw = calc_max_input_float_for_skin(max_adj_limit, skin_min, skin_max)
                raw_float = extra.get("floatValue", 0)
                if max_raw is not None and raw_float > max_raw + 0.0001:
                    continue  # Skip float-violating input
            coll_inputs[coll_name].append(inp)

        # ==========================================
        # BUG #4 FIX: Consider ALL collections with outputs as potential targets,
        # including those already handled by single-collection (10+ inputs).
        # A collection with 10+ inputs might still be better mixed with fillers
        # from a high-value-output collection.
        # ==========================================
        for target_coll in colls_with_outputs.get(out_rarity, set()):
            target_normal = coll_inputs.get(target_coll, [])

            # Need at least 1 input from target
            if len(target_normal) < 1:
                continue

            # Enumerate ALL split ratios: 1 to min(9, available) inputs from target
            max_from_target = min(len(target_normal), 9)

            # Early pruning: skip targets with no output value at all
            target_ev_per_slot = ev_per_input.get((target_coll, out_rarity), 0)
            if target_ev_per_slot <= 0:
                skipped_no_outputs += 1
                continue

            for n_target in range(1, max_from_target + 1):
                needed = 10 - n_target
                if needed <= 0:
                    continue

                target_selected = target_normal[:n_target]
                target_cost = sum(x.get("_best_price", 0) for x in target_selected)
                target_listing_ids = {x.get("_listing_id", "") for x in target_selected}

                # Early pruning: even with free fillers, target EV alone can't reach MIN_ROI
                target_ev_contribution = target_ev_per_slot * n_target
                # Upper bound: assume best possible fillers add their max ev_per_input
                # If target contribution alone < target_cost * 0.5, skip (fillers rarely make up 2x)
                if target_ev_contribution < target_cost * 0.5 and n_target >= 5:
                    continue

                # Collect candidate fillers by collection (same logic, preserving float checks)
                filler_by_coll = defaultdict(list)  # coll -> [inp, ...]
                for inp in pool:
                    inp_item = inp[0] if isinstance(inp, tuple) else inp
                    inp_coll = inp[1] if isinstance(inp, tuple) else None
                    if inp_coll is None:
                        continue
                    if inp_coll == target_coll:
                        continue
                    lid = inp_item.get("_listing_id", "")
                    if lid in target_listing_ids:
                        continue
                    # Check float violation for this filler's own collection
                    filler_max_adj = coll_max_adjusted.get((inp_coll, out_rarity))
                    if filler_max_adj is not None:
                        extra = inp_item.get("extra", {})
                        skin_min = extra.get("skin_min", 0)
                        skin_max = extra.get("skin_max", 1)
                        max_raw = calc_max_input_float_for_skin(filler_max_adj, skin_min, skin_max)
                        raw_float = extra.get("floatValue", 0)
                        if max_raw is not None and raw_float > max_raw + 0.0001:
                            continue
                    filler_by_coll[inp_coll].append(inp_item)

                if not filler_by_coll:
                    skipped_no_fillers += 1
                    continue

                # Try 3 filler strategies to find different profitable combos:
                # A) Cheapest fillers (minimize input cost)
                # B) Best EV-per-dollar (balance cost vs output value contribution)
                # C) Max jackpot (pick fillers from collections with highest single output)
                candidate_fills = []

                # Strategy A: cheapest average filler cost
                def _avg_cost(item):
                    f_coll, f_inputs = item
                    n = min(len(f_inputs), needed)
                    return -(sum(f.get("_best_price", 999999) for f in f_inputs[:n]) / n) if n > 0 else 0
                fillers_a = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _avg_cost)
                if len(fillers_a) >= needed:
                    candidate_fills.append(fillers_a)

                # Strategy B: best net EV per input (EV contribution minus cost)
                def _ev_per_dollar(item):
                    f_coll, f_inputs = item
                    ev_pi = ev_per_input.get((f_coll, out_rarity), 0)
                    n = min(len(f_inputs), needed)
                    avg_c = sum(f.get("_best_price", 999999) for f in f_inputs[:n]) / n if n > 0 else 999999
                    return ev_pi - avg_c
                fillers_b = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _ev_per_dollar)
                if len(fillers_b) >= needed:
                    # Dedup: only add if different from strategy A
                    ids_a = {f[0].get("_listing_id", "") for f in fillers_a}
                    ids_b = {f[0].get("_listing_id", "") for f in fillers_b}
                    if ids_b != ids_a:
                        candidate_fills.append(fillers_b)

                # Strategy C: max jackpot (highest single output price in filler collection)
                def _max_jackpot(item):
                    f_coll, _ = item
                    return max_single_output.get((f_coll, out_rarity), 0)
                fillers_c = _select_fillers(filler_by_coll, needed, target_listing_ids, MAX_COLLECTIONS - 1, _max_jackpot)
                if len(fillers_c) >= needed:
                    ids_c = {f[0].get("_listing_id", "") for f in fillers_c}
                    ids_prev = [{f[0].get("_listing_id", "") for f in fl} for fl in candidate_fills]
                    if ids_c not in ids_prev:
                        candidate_fills.append(fillers_c)

                if not candidate_fills:
                    skipped_no_fillers += 1
                    continue

                for fillers in candidate_fills:
                    # Combine all 10 inputs
                    all_10 = [(inp, target_coll) for inp in target_selected] + fillers

                    # Deduplicate: create a key from sorted listing IDs + target + ratio
                    listing_ids = sorted(x[0].get("_listing_id", "") for x in all_10)
                    # Filter out empty IDs to avoid false collisions (BUG #8 dedup fix)
                    non_empty_ids = [lid for lid in listing_ids if lid]
                    if len(non_empty_ids) < 10:
                        # Not enough unique IDs to reliably dedup — use price+float as fallback
                        dedup_key = tuple(sorted(
                            f"{x[0].get('_listing_id', '')}_{x[0].get('_best_price', 0)}_{x[0].get('extra', {}).get('floatValue', 0)}"
                            for x in all_10
                        ))
                    else:
                        dedup_key = tuple(non_empty_ids)
                    if dedup_key in seen_tradeups:
                        continue
                    seen_tradeups.add(dedup_key)

                    considered += 1
                    input_cost = sum(inp.get("_best_price", 0) for inp, _ in all_10)
                    if input_cost <= 0:
                        continue

                    # Group by collection and count
                    coll_counts = defaultdict(int)
                    for inp, coll in all_10:
                        coll_counts[coll] += 1

                    # Build input data for float calculation
                    input_data = []
                    for inp, _ in all_10:
                        extra = inp.get("extra", {})
                        input_data.append({
                            "float": extra.get("floatValue", 0.18),
                            "skin_min": extra.get("skin_min", 0.0),
                            "skin_max": extra.get("skin_max", 1.0),
                        })
                    avg_float = sum(d["float"] for d in input_data) / 10

                    # Calculate EV across all contributing collections
                    ev_sum = 0
                    out_info = []
                    has_price = False
                    has_missing = False
                    all_wwbs = True  # BUG #5: Track if ALL outputs are WW/BS

                    for coll, n_inputs in coll_counts.items():
                        outputs = coll_skins.get(coll, {}).get(out_rarity, [])
                        if not outputs:
                            continue
                        for out in outputs:
                            out_fv = calc_output_float(input_data, out["min_float"], out["max_float"])
                            out_cond = get_condition(out_fv)
                            cache_key = f"{out['name']}|{out_cond}"

                            # BUG #5: Track if any output is not WW/BS
                            if out_cond not in ("Well-Worn", "Battle-Scarred"):
                                all_wwbs = False

                            # Probability: (inputs from this coll / 10) * (1 / outputs in this coll)
                            prob = (n_inputs / 10) * (1 / len(outputs))

                            price = cached_prices.get(cache_key, 0)
                            if price and price > 0:
                                price_source = price_sources.get(cache_key, "Steam")
                                fee = SOURCE_FEES.get(price_source, 0.15)
                                price_after_fee = int(price * (1 - fee))
                                has_price = True
                            elif price == CSFLOAT_NO_LISTING:
                                # Try Skinport as sell platform (qty >= 2 guard)
                                sp_data = get_skinport_price(skinport_prices, out["name"], out_cond) if skinport_prices else None
                                if sp_data and sp_data.get("price", 0) > 0 and sp_data.get("quantity", 0) >= 2:
                                    price = sp_data["price"]
                                    price_source = "Skinport"
                                    fee = SOURCE_FEES.get("Skinport", 0.08)
                                    price_after_fee = int(price * (1 - fee))
                                    cached_prices[cache_key] = price
                                    price_sources[cache_key] = "Skinport"
                                    cache_timestamps[cache_key] = time.time()
                                    has_price = True
                                else:
                                    price_after_fee = 0
                                    price_source = "NO_LISTINGS"
                            else:
                                # BUG #6 FIX: Try on-the-fly Steam fetch for missing prices
                                trend = fetch_steam_trend(out["name"], out_cond)
                                if trend:
                                    steam_ref = trend.get("median") or trend.get("lowest")
                                    if steam_ref and steam_ref > 0:
                                        cached_prices[cache_key] = steam_ref
                                        price_sources[cache_key] = "Steam"
                                        cache_timestamps[cache_key] = time.time()
                                        if trend.get("volume_24h") is not None:
                                            cache_volumes[cache_key] = trend["volume_24h"]
                                        price = steam_ref
                                        price_source = "Steam"
                                        fee = SOURCE_FEES.get("Steam", 0.15)
                                        price_after_fee = int(price * (1 - fee))
                                        has_price = True
                                    else:
                                        price_after_fee = 0
                                        price_source = "none"
                                        has_missing = True
                                else:
                                    price_after_fee = 0
                                    price_source = "none"
                                    has_missing = True

                            # Apply liquidity discount
                            volume = cache_volumes.get(cache_key)
                            liq_mult = liquidity_multiplier(volume)
                            if price_after_fee > 0:
                                price_after_fee = int(price_after_fee * liq_mult)

                            ev_sum += price_after_fee * prob
                            out_info.append({
                                "name": out["name"],
                                "condition": out_cond,
                                "float": out_fv,
                                "float_min": out["min_float"],
                                "float_max": out["max_float"],
                                "price_raw": price if price and price > 0 else 0,
                                "price_after_fee": price_after_fee,
                                "price_source": price_source,
                                "probability": prob,
                                "from_collection": coll,
                                "volume_24h": volume,
                                "liquidity_mult": liq_mult,
                            })

                    if not has_price:
                        skipped_no_price += 1
                        continue

                    # BUG #5 FIX: Skip trade-ups where ALL outputs are WW/BS
                    if all_wwbs:
                        skipped_wwbs += 1
                        continue

                    net_ev = ev_sum - input_cost
                    roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

                    if roi < MIN_ROI or net_ev < MIN_EV:
                        continue

                    # Determine best output condition
                    priced_outputs = [o for o in out_info if o["price_after_fee"] > 0]
                    best_cond = priced_outputs[0]["condition"] if priced_outputs else "Unknown"

                    # Calculate max adjusted float for display
                    filter_target = 0.38
                    min_max_adjusted = None
                    for coll, n_inputs in coll_counts.items():
                        for out in coll_skins.get(coll, {}).get(out_rarity, []):
                            max_adj = calc_max_adjusted_float(out["min_float"], out["max_float"], filter_target)
                            if max_adj is not None:
                                if min_max_adjusted is None or max_adj < min_max_adjusted:
                                    min_max_adjusted = max_adj

                    # Build input list in result format (float violations already pre-filtered)
                    inputs_list = []
                    for inp, coll in all_10:
                        extra = inp.get("extra", {})
                        skin_min = extra.get("skin_min", 0)
                        skin_max = extra.get("skin_max", 1)
                        skin_range = skin_max - skin_min
                        raw_float = extra.get("floatValue", 0)
                        adjusted_float = (raw_float - skin_min) / skin_range if skin_range > 0 else 0
                        max_raw = calc_max_input_float_for_skin(min_max_adjusted, skin_min, skin_max) if min_max_adjusted else None
                        inputs_list.append({
                            "title": inp.get("title"),
                            "price": inp.get("_best_price", 0),
                            "source": inp.get("_best_source", "?"),
                            "original_source": inp.get("_original_source", inp.get("_best_source", "?")),
                            "price_from_skinport": inp.get("_price_from_skinport", False),
                            "float": raw_float,
                            "adjusted_float": adjusted_float,
                            "skin_min": skin_min,
                            "skin_max": skin_max,
                            "max_float": max_raw,
                            "listing_id": inp.get("_listing_id", ""),
                            "from_collection": coll,
                        })

                    has_csfloat_price = any(o.get("price_source") == "CSFloat" for o in out_info if o["price_raw"] > 0)

                    # Build collection label: "target + filler1 + filler2"
                    filler_names = sorted(c for c in coll_counts if c != target_coll)
                    coll_label = f"{target_coll} + {' + '.join(filler_names)}"
                    mix_desc = ", ".join(f"{c}({n})" for c, n in sorted(coll_counts.items(), key=lambda x: -x[1]))

                    results.append({
                        "collection": coll_label,
                        "target_collection": target_coll,
                        "coll_counts": dict(coll_counts),
                        "coll_mix": mix_desc,
                        "in_rarity": in_rarity,
                        "out_rarity": out_rarity,
                        "target_condition": best_cond,
                        "input_cost": input_cost,
                        "avg_float": avg_float,
                        "max_adjusted": min_max_adjusted,
                        "ev_output": ev_sum,
                        "ev": net_ev,
                        "roi": roi,
                        "is_stattrak": False,
                        "outputs": out_info,
                        "inputs": inputs_list,
                        "unverifiable": has_missing,
                        "unverified_csfloat": not has_csfloat_price,
                        "multi_collection": True,
                    })

    results.sort(key=lambda x: x["roi"], reverse=True)
    print(f"   Considered {considered} multi-collection combinations")
    if skipped_no_fillers:
        print(f"   Skipped {skipped_no_fillers} — not enough fillers at same rarity")
    if skipped_no_price:
        print(f"   Skipped {skipped_no_price} — no output prices available")
    if skipped_wwbs:
        print(f"   Skipped {skipped_wwbs} — all outputs WW/BS (no value)")
    print(f"   Found {len(results)} profitable multi-collection trade-ups (>={MIN_ROI}% ROI, >=${MIN_EV/100:.2f} EV)")

    return results


# ============ PHASE 3: FETCH TRENDS FOR PROFITABLE ============

def fetch_steam_trend(skin_name, condition):
    """Fetch Steam 30-day trend data. Bails after too many 429s (total, not just consecutive)."""
    global _steam_consecutive_429s, _steam_blocked

    if _steam_blocked:
        return None

    market_hash_name = f"{skin_name} ({condition})"
    params = {"appid": 730, "currency": 1, "market_hash_name": market_hash_name}

    # Pace requests: wait 1.5s before each Steam call to avoid triggering rate limit
    time.sleep(1.5)

    for attempt in range(3):
        try:
            r = requests.get(STEAM_URL, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 429:
                _steam_consecutive_429s += 1
                # Bail after 6 total 429s (not consecutive — a single success shouldn't
                # reset the counter to 0 because Steam rate limits are per-IP/window)
                if _steam_consecutive_429s >= 6:
                    _steam_blocked = True
                    print(f"   [STEAM] Blocked after {_steam_consecutive_429s} total 429s — skipping all remaining Steam fetches")
                    print(f"   [STEAM] Falling back to Skinport + CSFloat prices only")
                    return None
                backoff = [5, 15, 30][attempt]
                print(f"   [STEAM] Rate limited, pausing {backoff}s... ({_steam_consecutive_429s} total 429s)")
                time.sleep(backoff)
                continue
            # Don't fully reset — decrement by 1 on success (acknowledges partial recovery)
            if _steam_consecutive_429s > 0:
                _steam_consecutive_429s -= 1
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
            return None
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
            return None
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
            listing_source = inp.get("original_source", inp.get("source", ""))
            listing_id = inp.get("listing_id", "")

            if listing_source == "DMarket" and listing_id:
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
            elif listing_source == "CSFloat" and listing_id:
                result_check = verify_csfloat_listing(listing_id)
                if result_check["available"]:
                    inp["_verified"] = True
                    verified_count += 1
                else:
                    inp["_verified"] = False
                    inp["_sold"] = True
                    sold_count += 1
                time.sleep(0.2)
            elif listing_source == "Waxpeer" and listing_id:
                # Waxpeer listings can't be verified via API — mark as unverified
                inp["_verified"] = None
                unknown_count += 1
            else:
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

            # Fetch fresh listings from DMarket + search Waxpeer/CSFloat caches
            fresh_listings = []
            for skin_name in skin_titles:
                items = fetch_skin_raw(skin_name, max_items=50)
                fresh_listings.extend(items)

            # Also search Waxpeer cache for replacements
            waxpeer_items, _, waxpeer_status = load_waxpeer_cache()
            if waxpeer_items:
                for item in waxpeer_items:
                    item_name = extract_skin_name(item.get("title", ""))
                    if item_name in skin_titles:
                        fresh_listings.append(item)

            # Also search CSFloat input cache for replacements
            csfloat_items, _, csfloat_status = load_csfloat_input_cache()
            if csfloat_items:
                for item in csfloat_items:
                    item_name = extract_skin_name(item.get("title", ""))
                    if item_name in skin_titles:
                        fresh_listings.append(item)

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
                        "source": item.get("source", "DMarket"),
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

    if _steam_blocked:
        print("   Steam blocked — skipping trend data")
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
    print("CS2 TRADE-UP EV CALCULATOR v3.9")
    print("Inputs: DMarket+CSFloat+Waxpeer+Skinport | Outputs: Two-Pass (Broad Scan + CSFloat Verify)")
    num_keys = _csfloat_multi_limiter.key_count()
    total_budget = num_keys * 200
    print(f"CSFloat: {num_keys} key(s), budget {CSFLOAT_INPUT_CAP} inputs / {CSFLOAT_OUTPUT_RESERVE} outputs / {CSFLOAT_BUDGET_RESERVE} reserve = {total_budget}")
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

    # Load cache (v2: per-key timestamps)
    cached_prices, cache_timestamps, cached_sources, cache_volumes = load_cache()
    if cached_prices:
        fresh_count = sum(1 for t in cache_timestamps.values() if is_entry_fresh(t))
        stale_count = len(cached_prices) - fresh_count
        print(f"[CACHE] Loaded {len(cached_prices)} prices ({fresh_count} fresh, {stale_count} stale)")
    else:
        cache_timestamps = {}
        cached_sources = {}
        cache_volumes = {}
        print("[CACHE] Starting fresh")

    # Check saved opportunities first
    confirmed_opps, expired_opps = verify_saved_opportunities(cached_prices, cache_volumes)

    # PHASE 0: Reverse search (top-down from valuable outputs)
    priority_collections = phase0_reverse_search(coll_skins, cached_prices, cached_sources, cache_volumes)

    # PHASE 1: Fetch inputs
    viable_collections, watchlist_collections, all_inputs_by_collection, all_items, skin_db_lookup = phase1_fetch_inputs(float_limits, skinport_prices, skin_float_ranges, coll_skins)

    # PHASE 1b: Waxpeer targeted fetch for near-viable collections
    if watchlist_collections and WAXPEER_API_KEY:
        print("\n" + "-" * 70)
        print("PHASE 1b: Targeted Waxpeer fetch for near-viable collections")
        print("-" * 70)
        targeted_items = waxpeer_targeted_fetch(watchlist_collections, coll_skins, skin_db_lookup, float_limits)
        if targeted_items:
            # Process and merge targeted items
            targeted_processed = process_cached_items(targeted_items, float_limits, skinport_prices, skin_float_ranges)
            if targeted_processed:
                merged_items = all_items + targeted_processed
                prev_viable = len(viable_collections)
                prev_watchlist = len(watchlist_collections)
                viable_collections, watchlist_collections, all_inputs_by_collection = _classify_collections(merged_items)
                promoted = len(viable_collections) - prev_viable
                if promoted > 0:
                    print(f"   {promoted} collections promoted from watchlist to viable!")
                print(f"   Viable: {len(viable_collections)} (was {prev_viable}), Watchlist: {len(watchlist_collections)} (was {prev_watchlist})")

    if not viable_collections and not watchlist_collections:
        print("\nNo viable or watchlist collections found")
        return

    # PHASE 2 PASS 1: Broad scan (free data only)
    candidates, outputs_to_verify = phase2_pass1_broad_scan(
        viable_collections, all_inputs_by_collection, coll_skins,
        cached_prices, cached_sources, skinport_prices, skin_float_ranges,
        cache_timestamps, cache_volumes
    )

    print(f"\n   SCREENING RESULTS: {len(candidates)} candidates identified, {len(outputs_to_verify)} unique outputs to verify")

    # PHASE 2 PASS 2: Deep verify (CSFloat-primary)
    results, cached_prices, price_sources, cache_timestamps, cache_volumes = phase2_pass2_deep_verify(
        candidates, outputs_to_verify, coll_skins,
        cached_prices, cached_sources, skinport_prices, skin_float_ranges,
        cache_timestamps, cache_volumes
    )
    save_cache(cached_prices, price_sources, cache_timestamps, cache_volumes)

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
    multi_count = sum(1 for r in profitable if r.get("multi_collection"))
    single_count = len(profitable) - multi_count
    unverifiable_note = f", {len(unverifiable)} excluded (missing output prices)" if unverifiable else ""
    multi_note = f" ({single_count} single, {multi_count} multi-coll)" if multi_count else ""
    print(f"VERIFIED RESULTS: {len(results)} trade-ups verified, {len(all_profitable)} profitable, {len(meets_roi)} with ROI >= {MIN_ROI}%, {len(profitable)} with EV >= ${MIN_EV/100:.2f}{multi_note}{unverifiable_note}")
    print("=" * 70)

    if profitable:
        print("\n*** PROFITABLE TRADE-UPS ***\n")
        for r in profitable[:10]:
            print("=" * 80)
            st_tag = " [STATTRAK]" if r.get("is_stattrak") else ""
            csfloat_tag = " !! UNVERIFIED ON CSFLOAT !!" if r.get("unverified_csfloat") else ""
            print(f"  [{r['collection'].upper()}]{st_tag} +{r['roi']:.1f}% ROI | EV: ${r['ev']/100:.2f}{csfloat_tag}")
            print("=" * 80)
            if r.get("unverified_csfloat"):
                sources = set(o.get("price_source", "?") for o in r["outputs"] if o["price_raw"] > 0)
                print(f"  WARNING: All output prices from {', '.join(sources)} — not verified on CSFloat")
            print(f"  Rarity: {RARITY_NAMES[r['in_rarity']].upper()} -> {RARITY_NAMES[r['out_rarity']].upper()}")
            if r.get("multi_collection"):
                print(f"  Collection Mix: {r.get('coll_mix', '?')}")
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
                coll_tag = f" [{out['from_collection']}]" if out.get("from_collection") and r.get("multi_collection") else ""
                print(f"\n    [{out['probability']*100:.1f}% chance] {out['name']}{coll_tag}")
                print(f"    Condition: {out['condition']} (expected output float: {out['float']:.4f})")
                print(f"    Skin Float Range: {out['float_min']:.2f} - {out['float_max']:.2f}")

                if out['price_raw'] > 0:
                    source = out.get('price_source', 'CSFloat')
                    fee_pct = {"Steam": 15, "Skinport": 8, "CSFloat": 2}.get(source, 15)
                    liq_mult = out.get('liquidity_mult', 1.0)
                    liq_tag = ""
                    if liq_mult < 1.0:
                        liq_tag = f" [liq ×{liq_mult:.2f}]"
                    print(f"    Price [{source} -{fee_pct}%]: ${out['price_raw']/100:.2f} (${out['price_after_fee']/100:.2f} after fee{liq_tag})")
                    ev_contribution = out['price_after_fee'] * out['probability']
                    print(f"    EV Contribution: ${ev_contribution/100:.2f} ({out['probability']*100:.0f}% × ${out['price_after_fee']/100:.2f})")
                    vol = out.get('volume_24h')
                    if vol is not None:
                        vol_label = "HIGH" if vol >= 100 else "OK" if vol >= 10 else "LOW" if vol >= 2 else "VERY LOW"
                        print(f"    Liquidity: {vol}/day ({vol_label})")
                    # Fee-aware sell platform recommendation
                    raw = out['price_raw']
                    sell_options = [
                        ("CSFloat", int(raw * 0.98)),
                        ("Skinport", int(raw * 0.92)),
                        ("Steam", int(raw * 0.85)),
                    ]
                    sell_options.sort(key=lambda x: x[1], reverse=True)
                    sell_parts = [f"{name} ${net/100:.2f}" for name, net in sell_options]
                    print(f"    SELL ON: {sell_parts[0]} > {sell_parts[1]} > {sell_parts[2]}")
                    search_name = out['name'].replace(' ', '%20').replace('|', '%7C')
                    print(f"    CSFloat: https://csfloat.com/search?market_hash_name={search_name}%20%28{out['condition'].replace(' ', '%20')}%29&sort_by=lowest_price")
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
                    print(f"    Direct listings:")
                    for src, lid, price, flt in data["listings"]:
                        if src == "CSFloat":
                            print(f"      [{src}] ${price/100:.2f} @ {flt:.4f}: https://csfloat.com/item/{lid}")
                        elif src == "DMarket":
                            print(f"      [{src}] ${price/100:.2f} @ {flt:.4f}: https://dmarket.com/ingame-items/item-list/csgo-skins?userOfferId={lid}")
                        elif src == "Waxpeer" and lid.startswith("waxpeer_"):
                            waxpeer_id = lid[len("waxpeer_"):]
                            print(f"      [{src}] ${price/100:.2f} @ {flt:.4f}: https://waxpeer.com/item/{waxpeer_id}")

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
