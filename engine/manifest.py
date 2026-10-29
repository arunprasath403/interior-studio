"""Assemble the room manifest consumed by the generator and the viewer."""
from __future__ import annotations

import math
import zlib
from collections import defaultdict

from dataclasses import replace

from . import layout
from .budget import CATEGORY_SHARE, Item, Preferences, build_bom
from .currency import rate as currency_rate
from .prompts import build_prompt, seed_for

# Pitch (radians) per category so tags sit roughly where such items appear in a panorama.
_PITCH = {"furniture": -0.25, "fixed": -0.05, "lighting": 0.55, "decor": -0.4}

# The living room is the hub; every other panorama room links to it.
_HUB = "living"


def _yaw(index: int, count: int, offset: float = 0.0) -> float:
    """Evenly spread yaw in (-pi, pi]."""
    value = (index / count) * 2 * math.pi + offset
    return round(((value + math.pi) % (2 * math.pi)) - math.pi, 4)


def _links(rooms: list[layout.Room]) -> dict[str, list[str]]:
    hub = next((r.room_id for r in rooms if r.kind == _HUB), rooms[0].room_id)
    links: dict[str, list[str]] = defaultdict(list)
    for room in rooms:
        if room.room_id != hub:
            links[hub].append(room.room_id)
            links[room.room_id].append(hub)
    return links


def build_manifest(total_budget: float, total_area_sqm: float, bhk: str, style: str,
                   currency: str = "USD", seed: int = 42, design: dict | None = None) -> dict:
    """``design`` is the validated output of the design agent (see engine.agents), or None for pure rules.

    It maps room_id -> {"description": str, "exclude": frozenset, "priority": list}. It can reshape
    which items are chosen and how rooms are described, but the budget ceiling is still enforced here.
    """
    design = design or {}
    rooms = layout.allocate(total_area_sqm, bhk)
    prefs = Preferences(
        priorities={rid: list(d.get("priority", [])) for rid, d in design.items()},
        excludes={rid: frozenset(d.get("exclude", ())) for rid, d in design.items()},
    )
    fx = currency_rate(currency)
    usd_items, tier = build_bom(rooms, total_budget / fx, total_area_sqm, prefs)  # the catalog is priced in USD
    # Round down to cents so converting back can never push the total over the budget.
    items = [replace(i, cost=math.floor(i.cost * fx * 100) / 100) for i in usd_items]
    by_room: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_room[item.room_id].append(item)

    panorama_rooms = layout.visible_rooms(rooms)
    names = {r.room_id: r.name for r in panorama_rooms}
    links = _links(panorama_rooms)

    nodes = []
    for room in panorama_rooms:
        room_items = by_room[room.room_id]
        prompt = build_prompt(room, room_items, style, design.get(room.room_id, {}).get("description", ""))

        # Tags take even yaw slots; teleports sit in the gaps between them.
        slots = len(room_items) + len(links[room.room_id])
        hotspots = []
        for n, item in enumerate(room_items):
            hotspots.append({
                "id": f"{room.room_id}_{item.item_id}",
                "type": "product_tag",
                "coordinates": {"yaw": _yaw(n * 2, slots * 2), "pitch": _PITCH[item.category]},
                "payload": {"title": item.title, "cost": item.cost, "category": item.category,
                            "material": item.material},
            })
        for n, target in enumerate(links[room.room_id]):
            hotspots.append({
                "id": f"{room.room_id}_to_{target}",
                "type": "teleport",
                "coordinates": {"yaw": _yaw(len(room_items) * 2 + n * 2 + 1, slots * 2), "pitch": -0.05},
                "payload": {"target_room_id": target, "label": f"Go to {names[target]}"},
            })

        nodes.append({
            "room_id": room.room_id,
            "room_name": room.name,
            "kind": room.kind,
            "description": design.get(room.room_id, {}).get("description", ""),
            "area_sqm": room.area_sqm,
            "dimensions_m": {"width": room.width_m, "depth": room.depth_m},
            "allocated_budget": round(sum(i.cost for i in room_items), 2),
            "diffusion_parameters": {**prompt, "seed": seed_for(seed, room.room_id)},
            "asset_url": f"panos/{room.room_id}_360.jpg",
            "hotspots": hotspots,
        })

    spent = round(sum(i.cost for i in items), 2)
    by_category = {cat: round(sum(i.cost for i in items if i.category == cat), 2) for cat in CATEGORY_SHARE}
    return {
        "project_id": f"proj_{zlib.crc32(f'{total_budget}:{total_area_sqm}:{bhk}:{style}:{seed}'.encode()):08x}",
        "metadata": {
            "total_area_sqm": total_area_sqm,
            "bhk": bhk,
            "total_budget": total_budget,
            "currency": currency,
            "aesthetic_theme": style,
            "tier": tier,
            "design_source": "agent" if design else "rules",
            "spent": spent,
            "remaining": round(total_budget - spent, 2),
            "spent_by_category": by_category,
        },
        "topology": {"nodes": nodes},
    }
