"""Offer calculator — pure formula, no I/O.

Formula:
    repairs   = sqft × repair_rate[condition_grade]
    avg_comp  = mean(lowest N comps)
    offer     = round_to_500((avg_comp − repairs) × multiplier)
"""
from __future__ import annotations

REPAIR_RATES: dict[str, int] = {
    'C1': 5,
    'C2': 20,
    'C3': 35,
    'C4': 55,
    'C5': 85,
    'C6': 125,
}

DEFAULT_MULTIPLIER = 0.90
DEFAULT_COMP_COUNT = 4


def estimate_repairs(sqft: int, condition_grade: str) -> int:
    rate = REPAIR_RATES[condition_grade]
    return sqft * rate


def calculate_offer(
    comps: list[float],
    sqft: int,
    condition_grade: str,
    multiplier: float = DEFAULT_MULTIPLIER,
    comp_count: int = DEFAULT_COMP_COUNT,
) -> dict:
    if not comps:
        raise ValueError("comps list cannot be empty")

    lowest = sorted(comps)[:comp_count]
    avg_comp = sum(lowest) / len(lowest)
    repair_cost = estimate_repairs(sqft, condition_grade)
    raw_offer = max(0, (avg_comp - repair_cost) * multiplier)
    offer_price = round(raw_offer / 500) * 500

    return {
        'offer_price': int(offer_price),
        'avg_comp': int(avg_comp),
        'repair_cost': repair_cost,
        'arv': int(avg_comp),
        'condition_grade': condition_grade,
        'multiplier': multiplier,
        'comp_count': len(lowest),
    }
