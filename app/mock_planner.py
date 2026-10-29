"""A planner that needs no AWS, for trying the UI (mock mode)."""
from __future__ import annotations

import re

from engine.manifest import build_manifest


class MockPlanner:
    """Pulls simple numbers out of a free-text brief with regular expressions instead of calling Bedrock."""

    def plan(self, request: dict) -> dict:
        if "brief" not in request:
            return build_manifest(request["budget"], request["area_sqm"], request["bhk"], request["style"],
                                  request["currency"])
        text = request["brief"].lower()
        bhk = re.search(r"([1-5])\s*(?:bhk|bed(?:room)?s?)\b", text)
        area = re.search(r"(\d+(?:\.\d+)?)\s*(?:sq\s*m|sqm|m2|m²|square met)", text)
        lakh = re.search(r"(\d+(?:\.\d+)?)\s*lakh", text)
        money = re.search(r"\$\s*(\d[\d,]*)", text)
        missing = [name for name, found in (("bedrooms", bhk), ("area", area), ("budget", lakh or money)) if not found]
        if missing:
            raise ValueError(f"the brief does not say: {', '.join(missing)} (mock mode reads '2BHK', '90 sqm', '30 lakh')")
        budget, currency = (float(lakh.group(1)) * 100_000, "INR") if lakh else (float(money.group(1).replace(",", "")), "USD")
        from engine.prompts import STYLES

        style = next((name for name in STYLES if name in text), "modern minimalist")
        return build_manifest(budget, float(area.group(1)), f"{bhk.group(1)}BHK", style, currency)
