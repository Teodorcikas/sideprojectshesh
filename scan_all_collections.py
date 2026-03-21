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

# Load price cache
with open("price_cache.json", "r") as f:
    price_cache = json.load(f)
cached_output_prices = price_cache.get("prices", {})
print(f"Loaded {len(cached_output_prices)} cached output prices")


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

    # Get Skinport price
    sp_data = skinport_prices.get(title, {})
    sp_price = sp_data.get("price") if sp_data else None

    if sp_price and sp_price < price:
        best_price = sp_price
        source = "SP"
    else:
        best_price = price
        source = "DM"

    # Skip StatTrak for simplicity
    if "StatTrak" in title:
        continue

    skin_name = extract_skin_name(title)
    skin_range = skin_float_ranges.get(skin_name, {"min_float": 0.0, "max_float": 1.0})

    items_by_coll[coll][rarity].append({
        "title": title,
        "skin_name": skin_name,
        "float": fv,
        "price": best_price,
        "source": source,
        "skin_min": skin_range["min_float"],
        "skin_max": skin_range["max_float"],
    })

print(f"\nAnalyzing {len(coll_skins)} collections...")
print()

results = []

for coll_name, rarities in coll_skins.items():
    for out_rarity in range(1, 6):  # mil-spec to covert
        in_rarity = out_rarity - 1

        outputs = rarities.get(out_rarity, [])
        if not outputs:
            continue

        # Calculate max adjusted for this collection+rarity
        min_max_adj = None
        for out in outputs:
            max_adj = calc_max_adjusted(out["min_float"], out["max_float"])
            if max_adj is not None:
                if min_max_adj is None or max_adj < min_max_adj:
                    min_max_adj = max_adj

        if min_max_adj is None:
            continue

        # Get available inputs
        available_inputs = items_by_coll.get(coll_name, {}).get(in_rarity, [])

        # Filter by max float for each skin
        viable_inputs = []
        for inp in available_inputs:
            max_raw = calc_max_raw(min_max_adj, inp["skin_min"], inp["skin_max"])
            if max_raw is not None and inp["float"] <= max_raw:
                viable_inputs.append(inp)

        if len(viable_inputs) < 1:
            continue

        # Sort by price, take cheapest 10 (or fewer if not enough)
        viable_inputs.sort(key=lambda x: x["price"])
        top10 = viable_inputs[:10]

        # Calculate input cost and average adjusted float
        input_cost = sum(x["price"] for x in top10)

        adjusted_floats = []
        for x in top10:
            adj = (x["float"] - x["skin_min"]) / (x["skin_max"] - x["skin_min"]) if x["skin_max"] > x["skin_min"] else 0
            adjusted_floats.append(adj)
        avg_adjusted = sum(adjusted_floats) / len(adjusted_floats)

        # Calculate expected output value
        ev_sum = 0
        output_details = []
        for out in outputs:
            out_float = out["min_float"] + avg_adjusted * (out["max_float"] - out["min_float"])

            # Determine condition
            if out_float < 0.07:
                cond = "Factory New"
            elif out_float < 0.15:
                cond = "Minimal Wear"
            elif out_float < 0.38:
                cond = "Field-Tested"
            else:
                cond = "Well-Worn"

            cache_key = f"{out['name']}|{cond}"
            price = cached_output_prices.get(cache_key, 0)
            price_after_fee = int(price * 0.98) if price else 0

            prob = 1 / len(outputs)
            ev_sum += price_after_fee * prob
            output_details.append({
                "name": out["name"],
                "cond": cond,
                "float": out_float,
                "price": price_after_fee,
            })

        net_ev = ev_sum - input_cost
        roi = (net_ev / input_cost * 100) if input_cost > 0 else 0

        results.append({
            "collection": coll_name,
            "in_rarity": in_rarity,
            "out_rarity": out_rarity,
            "input_count": len(top10),
            "input_cost": input_cost,
            "avg_adjusted": avg_adjusted,
            "ev_output": ev_sum,
            "net_ev": net_ev,
            "roi": roi,
            "max_adj_limit": min_max_adj,
            "outputs": output_details,
            "inputs": top10[:3],
        })

# Sort by EV (descending)
results.sort(key=lambda x: x["net_ev"], reverse=True)

print("=" * 110)
print(f"ALL COLLECTIONS RANKED BY EV ({len(results)} trade-ups with viable inputs)")
print("=" * 110)
print()
print(f"{'Collection':<22} {'Trade-Up':<12} {'#':<3} {'Input$':<8} {'Out$':<8} {'EV':<10} {'ROI':<9} {'MaxAdj':<7} Output Conditions")
print("-" * 110)

for r in results:
    in_name = RARITY_NAMES[r["in_rarity"]][:3]
    out_name = RARITY_NAMES[r["out_rarity"]][:3]
    trade = f"{in_name}->{out_name}"

    # Output conditions summary
    out_conds = []
    for o in r["outputs"]:
        cond_short = o["cond"][:2]
        out_conds.append(f"{cond_short}:${o['price']/100:.0f}" if o["price"] else f"{cond_short}:?")
    out_str = ", ".join(out_conds[:4])
    if len(r["outputs"]) > 4:
        out_str += f" +{len(r['outputs'])-4}"

    ev_str = f"${r['net_ev']/100:+.2f}"
    roi_str = f"{r['roi']:+.1f}%"

    print(f"{r['collection']:<22} {trade:<12} {r['input_count']:<3} ${r['input_cost']/100:<7.2f} ${r['ev_output']/100:<7.2f} {ev_str:<10} {roi_str:<9} {r['max_adj_limit']:.3f}   {out_str}")

# Summary
print()
print("=" * 110)
profitable = [r for r in results if r["net_ev"] > 0]
print(f"PROFITABLE: {len(profitable)} | TOTAL ANALYZED: {len(results)}")
print()
print("Legend: #=available inputs (need 10), MaxAdj=max adjusted float limit for MW output")
print("        MW=Minimal Wear, FT=Field-Tested, FN=Factory New, ?=no cached price")
print()
print("NOTE: Many show $0 output because prices aren't cached yet. Run main program to fetch prices.")
