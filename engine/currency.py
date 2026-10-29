"""Currency units per 1 USD. The item catalog is priced in USD, so budgets are converted to USD to plan.

These rates are APPROXIMATE and fixed, not live. Edit them to match the rates you want to plan with.
"""
from __future__ import annotations

PER_USD = {
    "USD": 1.0,
    "INR": 85.0,
    "EUR": 0.92,
    "GBP": 0.78,
    "AED": 3.67,
    "CAD": 1.37,
    "AUD": 1.50,
    "SGD": 1.34,
}


def rate(currency: str) -> float:
    try:
        return PER_USD[currency.upper()]
    except KeyError:
        raise ValueError(f"unsupported currency {currency!r}; supported: {', '.join(PER_USD)}") from None
