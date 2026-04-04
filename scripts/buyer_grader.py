"""
scripts/buyer_grader.py — Buyer grade and response rate recalculation.

Grade rules:
  A: deal_count >= 3 OR response_rate >= 60.0
  B: deal_count >= 1 OR response_rate >= 30.0
  C: responded to at least one blast (response_rate > 0, deal_count == 0)
  D: never responded (default on import)

Import and call recalc_buyer_grade(buyer_id, sb) after every dispo_blasts
outcome update (e.g. reply to a blast) or after deal_count increments on close.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def calculate_grade(deal_count: int, response_rate: float) -> str:
    """Pure function — derive A/B/C/D from deal history metrics."""
    if deal_count >= 3 or response_rate >= 60.0:
        return "A"
    if deal_count >= 1 or response_rate >= 30.0:
        return "B"
    if response_rate > 0.0:
        return "C"
    return "D"


def recalc_buyer_grade(buyer_id: str, sb) -> dict:
    """
    Recompute response_rate and grade for a single buyer from blast history.

    Pulls the buyer's current deal_count, counts all dispo_blasts rows, and
    counts how many had a positive or unclear outcome (= responded).

    Writes response_rate, grade, and grade_updated_at back to cash_buyers.

    Returns the updated fields dict, or {} if the buyer was not found.
    """
    buyer_row = (
        sb.table("cash_buyers")
        .select("deal_count")
        .eq("id", buyer_id)
        .maybe_single()
        .execute()
    )
    if not buyer_row.data:
        log.warning("recalc_buyer_grade: buyer %s not found", buyer_id)
        return {}

    deal_count = int(buyer_row.data.get("deal_count") or 0)

    blasts = (
        sb.table("dispo_blasts")
        .select("outcome")
        .eq("buyer_id", buyer_id)
        .execute()
        .data
    )

    total_blasts = len(blasts)
    responded = sum(
        1 for b in blasts
        if b.get("outcome") in ("positive", "unclear")
    )

    response_rate = round(responded / total_blasts * 100, 2) if total_blasts else 0.0
    grade = calculate_grade(deal_count, response_rate)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        sb.table("cash_buyers").update({
            "response_rate":    response_rate,
            "grade":            grade,
            "grade_updated_at": now_iso,
        }).eq("id", buyer_id).execute()

        log.info(
            "Grade recalc buyer=%s blasts=%d responded=%d rate=%.1f%% grade=%s deal_count=%d",
            buyer_id, total_blasts, responded, response_rate, grade, deal_count,
        )
    except Exception as exc:
        log.error("Failed to update grade for buyer %s: %s", buyer_id, exc)
        return {}

    return {"grade": grade, "response_rate": response_rate, "deal_count": deal_count}
