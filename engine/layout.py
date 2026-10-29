"""Space allocation: split a total floor area into rooms with dimensions."""
from __future__ import annotations

import math
from dataclasses import dataclass

# Share of total area per room kind. Normalised, so the weights only need to be relative.
_WEIGHTS = {
    "living": 0.35,
    "kitchen": 0.15,
    "master_bedroom": 0.25,
    "bedroom": 0.15,
    "bathroom": 0.06,
}

# Minimum usable area (m2) per kind; below this the plan is rejected.
_MIN_AREA = {
    "living": 10.0,
    "kitchen": 5.0,
    "master_bedroom": 9.0,
    "bedroom": 7.5,
    "bathroom": 2.5,
}

_ASPECT = {  # width : depth
    "living": 1.4,
    "kitchen": 1.3,
    "master_bedroom": 1.2,
    "bedroom": 1.2,
    "bathroom": 1.3,
}

CEILING_HEIGHT_M = 2.8


@dataclass(frozen=True)
class Room:
    room_id: str
    kind: str
    name: str
    area_sqm: float
    width_m: float
    depth_m: float


def parse_bhk(spec: str) -> int:
    """'2BHK' -> 2. Accepts '2 bhk', '2bhk', or a bare number."""
    digits = "".join(ch for ch in spec if ch.isdigit())
    if not digits:
        raise ValueError(f"cannot read bedroom count from {spec!r}")
    count = int(digits)
    if not 1 <= count <= 5:
        raise ValueError("bedroom count must be between 1 and 5")
    return count


def _kinds_for(bedrooms: int) -> list[str]:
    kinds = ["living", "kitchen", "master_bedroom"]
    kinds += ["bedroom"] * (bedrooms - 1)
    kinds += ["bathroom"] * max(1, bedrooms - 1)
    return kinds


def _dimensions(kind: str, area: float) -> tuple[float, float]:
    ratio = _ASPECT[kind]
    depth = math.sqrt(area / ratio)
    return round(depth * ratio, 2), round(depth, 2)


def allocate(total_area_sqm: float, bhk: str) -> list[Room]:
    """Split ``total_area_sqm`` across the rooms of a ``bhk`` home.

    Raises ValueError when the area cannot hold every room at its minimum size.
    """
    if total_area_sqm <= 0:
        raise ValueError("total area must be positive")

    kinds = _kinds_for(parse_bhk(bhk))
    total_weight = sum(_WEIGHTS[k] for k in kinds)

    rooms: list[Room] = []
    counters: dict[str, int] = {}
    for kind in kinds:
        counters[kind] = counters.get(kind, 0) + 1
        area = round(total_area_sqm * _WEIGHTS[kind] / total_weight, 2)
        if area < _MIN_AREA[kind]:
            raise ValueError(
                f"{total_area_sqm} m2 is too small for a {bhk}: "
                f"{kind} would get {area} m2 (minimum {_MIN_AREA[kind]})"
            )
        width, depth = _dimensions(kind, area)
        index = counters[kind]
        label = kind.replace("_", " ").title()
        rooms.append(
            Room(
                room_id=f"room_{kind}_{index:02d}",
                kind=kind,
                name=label if kinds.count(kind) == 1 else f"{label} {index}",
                area_sqm=area,
                width_m=width,
                depth_m=depth,
            )
        )
    return rooms


def visible_rooms(rooms: list[Room]) -> list[Room]:
    """Rooms that get a 360 panorama. Bathrooms are skipped to keep generation cost down."""
    return [r for r in rooms if r.kind != "bathroom"]
