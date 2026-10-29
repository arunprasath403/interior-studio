"""Validation of user requests, shared by the local app and the cloud backend."""
from __future__ import annotations

from . import layout
from .currency import PER_USD


class BadRequest(Exception):
    """The request is invalid; the message is written for the person who sent it."""


def clean_request(data: dict) -> dict:
    """Validate and normalise a plan request: either {"brief": ...} or the structured fields."""
    if not isinstance(data, dict):
        raise BadRequest("send a JSON object")
    brief = data.get("brief")
    if brief is not None:
        if not isinstance(brief, str) or not 10 <= len(brief.strip()) <= 2000:
            raise BadRequest("describe your home in 10 to 2000 characters")
        return {"brief": brief.strip()}

    def number(key: str, label: str, low: float, high: float) -> float:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise BadRequest(f"{label} must be a number between {low:g} and {high:g}")
        return float(value)

    currency = str(data.get("currency", "USD")).upper()
    if currency not in PER_USD:
        raise BadRequest(f"currency must be one of {', '.join(PER_USD)}")
    style = str(data.get("style", "")).strip()
    if not 2 <= len(style) <= 80:
        raise BadRequest("style must be 2 to 80 characters")
    try:
        bhk = f"{layout.parse_bhk(str(data.get('bhk', '')))}BHK"
    except ValueError as err:
        raise BadRequest(f"bedrooms: {err}") from err
    return {"budget": number("budget", "budget", 100, 1e10), "area_sqm": number("area_sqm", "area", 15, 2000),
            "bhk": bhk, "style": style, "currency": currency}
