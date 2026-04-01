"""
dispo_tracks.py — Jenni Commercial Dispo System

Handles buyer matching, SMS blasting, reply classification, GHL pipeline
management, and post-qualification booking for the Commercial Dispo workflow.
"""
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GHL_BASE        = "https://services.leadconnectorhq.com"
GHL_API_KEY     = os.getenv("GHL_API_KEY", "")
GHL_HEADERS     = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "18Qc6ZWft7zdNY4oZUSm")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

JENNI_PHONE = os.getenv("JENNI_PHONE", "+14155390993")

COMMERCIAL_DEALS_CALENDAR_ID = "5wkxyL0IWbjseujZnF2l"
APPT_DURATION_MIN = 30

_dispo_pipeline_id: str = os.getenv("GHL_DISPO_PIPELINE_ID", "")
_dispo_stage_cache: dict = {}

DISPO_STAGES = [
    "Blast Sent",
    "Interest Confirmed",
    "Jenni Qualifying",
    "Call Scheduled",
    "LOI Submitted",
    "Closed",
    "Dead",
]

POSITIVE_KEYWORDS = {"yes", "interested", "sure", "tell me more", "send it", "call me", "details"}
NEGATIVE_KEYWORDS = {"no", "not interested", "pass", "remove", "stop"}

BLAST_SMS_TEMPLATE = (
    "Hey {first_name}, it's Jenni with Helpful Homebuyers.\n"
    "We just locked up a {property_type} in {city}, {state} — "
    "{unit_count} units, {cap_rate} cap, asking {asking_price}.\n"
    "Fits your buy criteria. Interested in seeing the numbers?\n"
    "Reply YES and I'll call you in the next hour."
)

# ── GHL HTTP helpers ──────────────────────────────────────────────────────────

def _ghl_request(method: str, path: str, retries: int = 3, **kwargs) -> Optional[requests.Response]:
    url = f"{GHL_BASE}{path}"
    kwargs.setdefault("headers", GHL_HEADERS)
    kwargs.setdefault("timeout", 15)
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("GHL rate limit on %s, retry in %ds", path, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < retries - 1:
                time.sleep(1)
                continue
            return r
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                log.error("GHL %s %s failed after %d attempts: %s", method, path, retries, exc)
    return None


def _ghl_get(path: str, **kw) -> Optional[requests.Response]:
    return _ghl_request("GET", path, **kw)


def _ghl_post(path: str, **kw) -> Optional[requests.Response]:
    return _ghl_request("POST", path, **kw)


def _ghl_put(path: str, **kw) -> Optional[requests.Response]:
    return _ghl_request("PUT", path, **kw)


# ── Supabase helper ───────────────────────────────────────────────────────────

def _get_sb():
    """Return a Supabase client. Raises RuntimeError if env vars missing."""
    from supabase import create_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_KEY) must be set"
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Classification ────────────────────────────────────────────────────────────

def classify_reply(message: str) -> str:
    """
    Classify an inbound SMS reply as 'positive', 'negative', or 'unclear'.

    Negative keywords take priority — if both appear, return 'negative'.
    """
    text = message.lower().strip()
    if any(kw in text for kw in NEGATIVE_KEYWORDS):
        return "negative"
    if any(kw in text for kw in POSITIVE_KEYWORDS):
        return "positive"
    return "unclear"


# ── Price formatting ──────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price >= 1_000_000:
        return f"${price / 1_000_000:.1f}M"
    if price >= 1_000:
        return f"${price / 1_000:.0f}K"
    return f"${price:.0f}"
