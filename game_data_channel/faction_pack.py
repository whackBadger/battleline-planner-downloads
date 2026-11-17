"""Project one English Field Manual faction into reproducible numeric game data."""
from __future__ import annotations

import json
import re
import unicodedata


def name_key(name):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", name).casefold())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checked_int(value, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid numeric price field")
    return value


def price_fields(unit):
    if unit.get("issue") or not unit["tiers"]:
        return None
    tiers = []
    for row in unit["tiers"]:
        start = checked_int(row["fromCopy"], 1)
        end = None if row["toCopy"] is None else checked_int(row["toCopy"], start)
        tier = {"fromCopy": start, "toCopy": end,
                "sizes": [{"models": checked_int(size["models"], 1), "points": checked_int(size["points"])}
                          for size in row["sizes"]]}
        if row.get("exactSizes"):
            tier["exactSizes"] = True
        if row.get("ambiguousSizes"):
            tier["ambiguousSizes"] = [checked_int(n, 1) for n in row["ambiguousSizes"]]
        tiers.append(tier)
    wargear = []
    for gear in unit["wargear"]:
        name = gear["name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 180:
            raise ValueError("Invalid wargear name")
        wargear.append({"name": name, "points": checked_int(gear["points"])})
    result = {"tiers": tiers, "wargear": wargear}
    if unit.get("wargearCostsRemoved"):
        result["wargearCostsRemoved"] = True
    return result


def faction_units(page):
    units = {}
    for unit in page["units"]:
        key = name_key(unit["name"])
        if not key:
            raise ValueError("Unrecognized unit name")
        price = price_fields(unit)
        if key in units and units[key] != price:
            # Native and allied variants can share a display name at different costs.
            price = None
        units[key] = price
    if not any(price is not None for price in units.values()):
        raise ValueError(f"No verified prices for {page['faction']}")
    return units


def encode_pack(slug, units):
    return (canonical({"schema": 1, "faction": slug, "units": units}) + "\n").encode("utf-8")


def build_faction_pack(page):
    return encode_pack(page["faction"], faction_units(page))
