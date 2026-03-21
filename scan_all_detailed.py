import requests
import json
from collections import defaultdict

SKINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"

RARITY_ORDER = {
    "consumer grade": 0, "industrial grade": 1, "mil-spec grade": 2,
    "restricted": 3, "classified": 4, "covert": 5,
}
RARITY_NAMES = ["consumer", "industrial", "mil-spec", "restricted", "classified", "covert"]
VALID_CATEGORIES = {"Rifles", "SMGs", "Pistols", "Heavy", "Equipment"}

print("Loading skin database...")
skins_data = requests.get(SKINS_URL).json()

# Build skin database
coll_skins = defaultdict(lambda: defaultdict(list))
skin_float_ranges = {}

for skin in skins_data:
    category = skin.get("category", {}).get("name", "")
    if category not in VALID_CATEGORIES:
        continue
    rarity = skin.get("rarity", {}).get("name", "").lower()
    if rarity not in RARITY_ORDER:
        continue
    collections = skin.get("collections", [])
    if not collections:
        continue

    name = skin.get("name", "")
    min_float = skin.get("min_float") or 0.0
    max_float = skin.get("max_float") or 1.0
    skin_float_ranges[name] = {"min_float": min_float, "max_float": max_float}

    for coll in collections:
        coll_name = coll.get("name", "").lower().replace("the ", "").replace(" collection", "").strip()
        coll_skins[coll_name][RARITY_ORDER[rarity]].append({
            "name": name,
            "min_float": min_float,
            "max_float": max_float,
        })

print(f"Loaded {len(coll_skins)} collections")

# Load DMarket cache
with open("dmarket_cache.json", "r") as f:
    cache_data = json.load(f)
raw_items = cache_data.get("items", [])
print(f"Loaded {len(raw_items)} DMarket items")

# Load Skinport cache
with open("skinport_cache.json", "r") as f:
    sp_cache = json.load(f)
skinport_prices = sp_cache.get("prices", {})
print(f"Loaded {len(skinport_prices)} Skinport prices")


def extract_skin_name(title):
    for cond in ["(Factory New)", "(Minimal Wear)", "(Field-Tested)", "(Well-Worn)", "(Battle-Scarred)"]:
        title = title.replace(cond, "")
    title = title.replace("StatTrak\u2122 ", "").replace("StatTrak ", "")
    return title.strip()


def calc_max_adjusted(out_min, out_max, target=0.15):
    if out_max <= out_min:
        return None
    val = (target - out_min) / (out_max - out_min)
    return val if val > 0 else None


def calc_max_raw(max_adj, skin_min, skin_max):
    if max_adj is None:
        return None
    max_raw = skin_min + max_adj * (skin_max - skin_min)
    return max_raw if max_raw >= 0.15 else None


# Process all items by collection
items_by_coll = defaultdict(lambda: defaultdict(list))

for item in raw_items:
    coll = item["collection"]
    rarity = RARITY_ORDER.get(item["quality"])
    if rarity is None:
        continue

    title = item["title"]
    fv = item["float"]
    price = item["price_usd"]

    sp_data = skinport_prices.get(title, {})
    sp_price = sp_data.get("price") if sp_data else None

    if sp_price and sp_price < price:
        best_price = sp_price
    else:
        best_price = price

    if "StatTrak" in title:
        continue

    skin_name = extract_skin_name(title)
    skin_range = skin_float_ranges.get(skin_name, {"min_float": 0.0, "max_float": 1.0})

    items_by_coll[coll][rarity].append({
        "title": title,
        "skin_name": skin_name,
        "float": fv,
        "price": best_price,
        "skin_min": skin_range["min_float"],
        "skin_max": skin_range["max_float"],
    })

print(f"\nAnalyzing all collections...\n")

results = []

for coll_name, rarities in coll_skins.items():
    for out_rarity in range(1, 6):
        in_rarity = out_rarity - 1

        outputs = rarities.get(out_rarity, [])
        if not outputs:
            continue

        inputs_in_db = rarities.get(in_rarity, [])
        if not inputs_in_db:
            continue

        # Calculate max adjusted for MW output
        min_max_adj = None
        limiting_output = None
        for out in outputs:
            max_adj = calc_max_adjusted(out["min_float"], out["max_float"])
            if max_adj is not None:
                if min_max_adj is None or max_adj < min_max_adj:
                    min_max_adj = max_adj
                    limiting_output = out["name"]

        if min_max_adj is None:
            results.append({
                "collection": coll_name,
                "trade": f"{RARITY_NAMES[in_rarity][:3]}->{RARITY_NAMES[out_rarity][:3]}",
                "status": "NO_MW_POSSIBLE",
                "reason": "Output skins can't be MW",
                "max_adj": None,
                "raw_inputs": 0,
                "viable_inputs": 0,
            })
            continue

        # Get raw inputs from market
        raw_inputs = items_by_coll.get(coll_name, {}).get(in_rarity, [])

        # Check each input
        viable_inputs = []
        rejected_max_raw_none = 0
        rejected_float_too_high = 0

        for inp in raw_inputs:
            max_raw = calc_max_raw(min_max_adj, inp["skin_min"], inp["skin_max"])
            if max_raw is None:
                rejected_max_raw_none += 1
                continue
            if inp["float"] > max_raw:
                rejected_float_too_high += 1
                continue
            viable_inputs.append(inp)

        # Determine status
        if len(raw_inputs) == 0:
            status = "NO_LISTINGS"
            reason = "No FT listings on market"
        elif len(viable_inputs) == 0:
            if rejected_max_raw_none > 0:
                status = "IMPOSSIBLE"
                reason = f"Max raw < 0.15 for all {rejected_max_raw_none} skins (FT can't produce MW)"
            else:
                status = "FLOAT_TOO_HIGH"
                reason = f"All {rejected_float_too_high} listings have float > max allowed"
        elif len(viable_inputs) < 10:
            status = "NEED_MORE"
            reason = f"Only {len(viable_inputs)}/10 viable inputs"
        else:
            status = "VIABLE"
            reason = f"{len(viable_inputs)} viable inputs"

        results.append({
            "collection": coll_name,
            "trade": f"{RARITY_NAMES[in_rarity][:3]}->{RARITY_NAMES[out_rarity][:3]}",
            "status": status,
            "reason": reason,
            "max_adj": min_max_adj,
            "limiting_output": limiting_output,
            "raw_inputs": len(raw_inputs),
            "viable_inputs": len(viable_inputs),
            "rejected_impossible": rejected_max_raw_none,
            "rejected_float": rejected_float_too_high,
        })

# Group by status
by_status = defaultdict(list)
for r in results:
    by_status[r["status"]].append(r)

print("=" * 100)
print("COLLECTION ANALYSIS BY STATUS")
print("=" * 100)

status_order = ["VIABLE", "NEED_MORE", "FLOAT_TOO_HIGH", "IMPOSSIBLE", "NO_LISTINGS", "NO_MW_POSSIBLE"]
for status in status_order:
    items = by_status.get(status, [])
    if not items:
        continue

    print(f"\n### {status} ({len(items)} trade-ups) ###\n")

    if status == "VIABLE":
        print(f"{'Collection':<25} {'Trade':<12} {'MaxAdj':<8} {'Viable':<8} Limiting Output")
        print("-" * 80)
        for r in sorted(items, key=lambda x: -x["viable_inputs"]):
            print(f"{r['collection']:<25} {r['trade']:<12} {r['max_adj']:.4f}   {r['viable_inputs']:<8} {r.get('limiting_output', '')}")

    elif status == "NEED_MORE":
        print(f"{'Collection':<25} {'Trade':<12} {'MaxAdj':<8} {'Have':<6} {'Raw':<6} Limiting Output")
        print("-" * 80)
        for r in sorted(items, key=lambda x: -x["viable_inputs"]):
            print(f"{r['collection']:<25} {r['trade']:<12} {r['max_adj']:.4f}   {r['viable_inputs']:<6} {r['raw_inputs']:<6} {r.get('limiting_output', '')}")

    elif status == "FLOAT_TOO_HIGH":
        print(f"{'Collection':<25} {'Trade':<12} {'MaxAdj':<8} {'Raw':<6} Reason")
        print("-" * 80)
        for r in sorted(items, key=lambda x: -x["max_adj"])[:15]:
            print(f"{r['collection']:<25} {r['trade']:<12} {r['max_adj']:.4f}   {r['raw_inputs']:<6} {r['reason']}")
        if len(items) > 15:
            print(f"  ... and {len(items)-15} more")

    elif status == "IMPOSSIBLE":
        print(f"{'Collection':<25} {'Trade':<12} {'MaxAdj':<8} {'Raw':<6} Limiting Output")
        print("-" * 80)
        for r in sorted(items, key=lambda x: -x["max_adj"])[:15]:
            print(f"{r['collection']:<25} {r['trade']:<12} {r['max_adj']:.4f}   {r['raw_inputs']:<6} {r.get('limiting_output', '')}")
        if len(items) > 15:
            print(f"  ... and {len(items)-15} more")

    elif status == "NO_LISTINGS":
        print(f"{'Collection':<25} {'Trade':<12} {'MaxAdj':<8}")
        print("-" * 60)
        for r in items[:10]:
            max_adj_str = f"{r['max_adj']:.4f}" if r['max_adj'] else "N/A"
            print(f"{r['collection']:<25} {r['trade']:<12} {max_adj_str}")
        if len(items) > 10:
            print(f"  ... and {len(items)-10} more")

print("\n" + "=" * 100)
print("SUMMARY")
print("=" * 100)
for status in status_order:
    count = len(by_status.get(status, []))
    print(f"  {status:<20}: {count}")
print(f"  {'TOTAL':<20}: {len(results)}")

# Show the key insight
print("\n" + "=" * 100)
print("KEY INSIGHT: Why so few viable trade-ups?")
print("=" * 100)
print("""
The October 2025 algorithm normalizes input float within the skin's full range:
  adjusted = (skin_float - skin_min) / (skin_max - skin_min)

For a typical skin with range 0.00-0.65:
  - FT minimum (0.15) has adjusted = 0.15/0.65 = 0.231
  - But MW output requires adjusted < ~0.15-0.22 depending on output skin

This means: Most skins with min_float=0.00 CANNOT produce MW outputs from FT inputs!

Only skins with HIGH min_float (like 0.06+) have a chance because:
  - Safari Mesh (0.06-0.80): adjusted at 0.15 = 0.09/0.74 = 0.122 ✓
  - Typical skin (0.00-0.65): adjusted at 0.15 = 0.15/0.65 = 0.231 ✗
""")
