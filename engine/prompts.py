"""Equirectangular prompt synthesis shared across rooms for visual continuity."""
from __future__ import annotations

import zlib

from .budget import Item
from .layout import CEILING_HEIGHT_M, Room

STYLES = {
    "modern minimalist": "modern minimalist, clean lines, neutral warm white walls, light oak floor, matte black accents",
    "dark academia": "dark academia, deep forest green and walnut tones, warm backlit shelves, brass fixtures, Persian rug",
    "scandinavian": "scandinavian, pale birch wood, soft white walls, wool textures, muted sage and grey accents",
    "industrial": "industrial loft, exposed brick, black steel frames, polished concrete floor, leather furniture",
    "japandi": "japandi, light ash wood, linen textures, low furniture, warm earth tones, paper-shade lighting",
}

_ROOM_NOUN = {
    "living": "living room",
    "kitchen": "modular kitchen",
    "master_bedroom": "master bedroom",
    "bedroom": "bedroom",
    "bathroom": "bathroom",
}

_FRAMING = (
    "seamless 360 equirectangular spherical panorama, 2:1 aspect ratio, "
    "camera at eye level (1.5 m) in the centre of the room"
)
_QUALITY = (
    "photorealistic interior photography, architectural render, physically correct global illumination, "
    "natural daylight through windows plus warm 2700K downlights, sharp detail, seamless horizontal wrap"
)
NEGATIVE = (
    "seam line, split image, distorted furniture, fisheye vignette, text, watermark, "
    "cartoon, low resolution, blurry, harsh fluorescent lighting"
)


def style_description(style: str) -> str:
    """Known styles expand to a palette; anything else is used verbatim."""
    return STYLES.get(style.strip().lower(), style.strip())


def seed_for(project_seed: int, room_id: str) -> int:
    return zlib.crc32(f"{project_seed}:{room_id}".encode()) & 0x7FFFFFFF


def build_prompt(room: Room, items: list[Item], style: str, description: str = "") -> dict:
    furnishings = ", ".join(i.title.lower() for i in items if i.category != "fixed")
    finishes = ", ".join(f"{i.material.lower()} {i.title.lower()}" for i in items if i.category == "fixed")
    parts = [
        f"{_FRAMING}",
        f"{_ROOM_NOUN[room.kind]} interior, {style_description(style)}",
        f"{room.area_sqm:g} square metres, {room.width_m:g} m by {room.depth_m:g} m, {CEILING_HEIGHT_M} m ceiling",
    ]
    if description:  # written by the design agent: concrete materials, colours and light for this room
        parts.append(description)
    if furnishings:
        parts.append(f"featuring {furnishings}")
    if finishes:
        parts.append(f"finishes: {finishes}")
    parts.append(_QUALITY)
    return {"positive_prompt": ", ".join(parts), "negative_prompt": NEGATIVE}
