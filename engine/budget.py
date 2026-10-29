"""Bill of materials: pick priced items per room without ever exceeding the budget."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .layout import Room

# Category split of the total budget (sums to 1.0).
CATEGORY_SHARE = {
    "fixed": 0.30,
    "furniture": 0.45,
    "lighting": 0.15,
    "decor": 0.10,
}

# Each entry: (item_id, title, category, base_price_per_sqm, material).
# Price = base_price_per_sqm * room area * tier multiplier, so cost scales with the room.
_CATALOG: dict[str, list[tuple[str, str, str, float, str]]] = {
    "living": [
        ("sofa", "L-shaped Sofa", "furniture", 14.0, "Performance linen"),
        ("tv_unit", "Wall TV Console", "furniture", 9.0, "Veneer on plywood"),
        ("coffee_table", "Coffee Table", "furniture", 4.0, "Solid wood"),
        ("pendant", "Statement Pendant Light", "lighting", 5.0, "Brass"),
        ("floor_lamp", "Arc Floor Lamp", "lighting", 2.5, "Metal"),
        ("rug", "Area Rug", "decor", 4.0, "Wool blend"),
        ("wall_art", "Framed Wall Art", "decor", 2.0, "Canvas"),
        ("flooring", "Flooring", "fixed", 12.0, "Engineered wood"),
        ("wall_finish", "Feature Wall Finish", "fixed", 6.0, "Textured paint"),
    ],
    "kitchen": [
        ("cabinets", "Modular Cabinets", "fixed", 30.0, "Laminate shutters"),
        ("countertop", "Countertop", "fixed", 14.0, "Quartz"),
        ("backsplash", "Backsplash", "fixed", 6.0, "Ceramic tile"),
        ("dining_set", "Breakfast Counter Set", "furniture", 12.0, "Solid wood"),
        ("under_cabinet", "Under-cabinet LED Strip", "lighting", 3.0, "LED"),
        ("pendant", "Pendant Lights", "lighting", 4.0, "Glass"),
        ("planter", "Herb Planter", "decor", 1.5, "Ceramic"),
    ],
    "master_bedroom": [
        ("bed", "King Bed with Headboard", "furniture", 16.0, "Upholstered"),
        ("wardrobe", "Built-in Wardrobe", "furniture", 14.0, "Laminate"),
        ("nightstands", "Bedside Tables (pair)", "furniture", 4.0, "Solid wood"),
        ("sconces", "Wall Sconces (pair)", "lighting", 3.0, "Brass"),
        ("cove_light", "Dimmable LED Cove", "lighting", 4.0, "LED"),
        ("rug", "Bedroom Rug", "decor", 3.0, "Wool blend"),
        ("curtains", "Curtains", "decor", 2.5, "Linen"),
        ("flooring", "Flooring", "fixed", 12.0, "Engineered wood"),
    ],
    "bedroom": [
        ("bed", "Queen Bed", "furniture", 13.0, "Upholstered"),
        ("wardrobe", "Wardrobe", "furniture", 11.0, "Laminate"),
        ("study_desk", "Study Desk", "furniture", 5.0, "Solid wood"),
        ("bedside_lamp", "Bedside Lamps", "lighting", 2.0, "Ceramic"),
        ("curtains", "Curtains", "decor", 2.0, "Linen"),
        ("flooring", "Flooring", "fixed", 11.0, "Vitrified tile"),
    ],
    "bathroom": [
        ("sanitaryware", "Sanitaryware Set", "fixed", 40.0, "Ceramic"),
        ("vanity", "Vanity Unit", "furniture", 12.0, "Moisture-resistant ply"),
        ("mirror_light", "Mirror with Light", "lighting", 6.0, "LED"),
    ],
}

# Per-category price multipliers are fitted to the budget, then clamped to this range. The upper
# clamp stops a huge budget from pricing a sofa absurdly; any unspent money is reported, not forced.
_MULT_MIN, _MULT_MAX = 0.5, 12.0

# (upper bound of the average multiplier, tier name). Walked from the top down.
_TIERS = [(1.0, "economy"), (2.5, "standard"), (4.5, "premium"), (float("inf"), "luxury")]


@dataclass(frozen=True)
class Item:
    item_id: str
    room_id: str
    title: str
    category: str
    cost: float
    material: str


@dataclass(frozen=True)
class Preferences:
    """Optional, untrusted hints from the design agent. They reshape the plan but can never raise the total."""

    priorities: dict[str, list[str]] = field(default_factory=dict)  # room_id -> item ids to take first
    excludes: dict[str, frozenset[str]] = field(default_factory=dict)  # room_id -> item ids to drop


def catalog_options(kind: str) -> list[tuple[str, str]]:
    """(item_id, title) pairs the planner may choose from for a room kind."""
    return [(item_id, title) for item_id, title, *_ in _CATALOG[kind]]


def catalog_materials(kind: str) -> dict[str, str]:
    """item_id -> material for a room kind, so descriptions can be kept consistent with what is priced."""
    return {item_id: material for item_id, _, _, _, material in _CATALOG[kind]}


def _catalog_for(room: Room, prefs: Preferences) -> list[tuple[str, str, str, float, str]]:
    """The room's catalog with excluded items removed and prioritised items moved to the front."""
    dropped = prefs.excludes.get(room.room_id, frozenset())
    entries = [e for e in _CATALOG[room.kind] if e[0] not in dropped]
    order = {item_id: n for n, item_id in enumerate(prefs.priorities.get(room.room_id, []))}
    return sorted(entries, key=lambda e: order.get(e[0], len(order)))  # stable: others keep catalog order


def _base_costs(rooms: list[Room], prefs: Preferences) -> dict[str, dict[str, float]]:
    """Catalog cost at multiplier 1.0, per room and category."""
    base: dict[str, dict[str, float]] = {}
    for room in rooms:
        per_category = {cat: 0.0 for cat in CATEGORY_SHARE}
        for _, _, category, per_sqm, _ in _catalog_for(room, prefs):
            per_category[category] += per_sqm * room.area_sqm
        base[room.room_id] = per_category
    return base


def _multipliers(base: dict[str, dict[str, float]], total_budget: float) -> dict[str, float]:
    """Price multiplier per category so the full catalog costs about that category's budget share."""
    multipliers = {}
    for cat, share in CATEGORY_SHARE.items():
        base_total = sum(per_room[cat] for per_room in base.values())
        if base_total == 0:  # every item of this category was excluded; nothing to price
            multipliers[cat] = _MULT_MIN
            continue
        multipliers[cat] = min(_MULT_MAX, max(_MULT_MIN, total_budget * share / base_total))
    return multipliers


def _tier_name(multipliers: dict[str, float]) -> str:
    average = sum(multipliers.values()) / len(multipliers)
    return next(name for bound, name in _TIERS if average < bound)


def build_bom(rooms: list[Room], total_budget: float, total_area_sqm: float,
              prefs: Preferences | None = None) -> tuple[list[Item], str]:
    """Pick items per room, never exceeding ``total_budget`` in total.

    Items are priced up or down to the budget, so a bigger budget buys better pieces rather than
    sitting unspent. Each category's budget is split across rooms by what the room needs (a kitchen
    needs more fixed cost than a bedroom), and items are taken in catalog order while they fit, so
    the first listed items are the priority ones.
    """
    if total_budget <= 0:
        raise ValueError("budget must be positive")

    prefs = prefs or Preferences()
    base = _base_costs(rooms, prefs)
    multipliers = _multipliers(base, total_budget)
    tier_name = _tier_name(multipliers)
    items: list[Item] = []

    for room in rooms:
        remaining = {
            cat: total_budget * share * base[room.room_id][cat] / (sum(b[cat] for b in base.values()) or 1.0)
            for cat, share in CATEGORY_SHARE.items()
        }
        for item_id, title, category, per_sqm, material in _catalog_for(room, prefs):
            # Round down to cents so rounding can never push the total over the budget.
            cost = math.floor(per_sqm * room.area_sqm * multipliers[category] * 100) / 100
            if cost <= remaining[category] + 1e-6:
                remaining[category] -= cost
                items.append(Item(item_id, room.room_id, title, category, cost, material))

    total = round(sum(i.cost for i in items), 2)
    if total > total_budget:  # unreachable by construction; guards future catalog edits
        raise AssertionError(f"bill of materials {total} exceeds budget {total_budget}")
    return items, tier_name
