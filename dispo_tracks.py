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
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

JENNI_PHONE = os.getenv("JENNI_PHONE", "")

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


# ── Buyer matching ─────────────────────────────────────────────────────────────

def match_buyers(deal_data: dict) -> list:
    """
    Query Supabase cash_buyers for active buyers whose criteria match the deal.

    Matching rules:
      - status = 'active'
      - price_range_min <= asking_price <= price_range_max
      - state in preferred_states
      - property_type substring match in buy_criteria->property_type (if provided)
    """
    sb = _get_sb()
    asking_price = float(deal_data.get("asking_price") or 0)
    state = (deal_data.get("state") or "").strip()
    property_type = (deal_data.get("property_type") or "").strip()

    rows = (
        sb.table("cash_buyers")
        .select("*")
        .eq("status", "active")
        .lte("price_range_min", asking_price)
        .gte("price_range_max", asking_price)
        .contains("preferred_states", [state])
        .execute()
        .data
    )

    if property_type:
        rows = [
            r for r in rows
            if property_type.lower() in (
                ((r.get("buy_criteria") or {}).get("property_type") or "").lower()
            )
        ]

    return rows


# ── GHL pipeline management ───────────────────────────────────────────────────

def ensure_dispo_pipeline() -> str:
    """
    Return the Commercial Dispo pipeline ID.

    Priority:
      1. GHL_DISPO_PIPELINE_ID env var
      2. Search GHL for existing pipeline named "Commercial Dispo"

    Raises RuntimeError if neither exists (pipeline must be created manually in GHL).
    """
    global _dispo_pipeline_id
    if _dispo_pipeline_id:
        return _dispo_pipeline_id

    r = _ghl_get("/opportunities/pipelines", params={"locationId": GHL_LOCATION_ID})
    if not r or r.status_code != 200:
        raise RuntimeError(
            f"Could not fetch GHL pipelines (status {r.status_code if r else 'no response'})"
        )

    for pipeline in r.json().get("pipelines", []):
        if pipeline.get("name", "").lower() == "commercial dispo":
            _dispo_pipeline_id = pipeline["id"]
            log.info("Found 'Commercial Dispo' pipeline: %s", _dispo_pipeline_id)
            _populate_stage_cache(pipeline)
            return _dispo_pipeline_id

    raise RuntimeError(
        "No 'Commercial Dispo' pipeline found in GHL and GHL_DISPO_PIPELINE_ID not set.\n"
        "Create it in GHL with these stages: Blast Sent, Interest Confirmed, Jenni Qualifying, "
        "Call Scheduled, LOI Submitted, Closed, Dead — then set GHL_DISPO_PIPELINE_ID in Render."
    )


def _populate_stage_cache(pipeline: dict) -> None:
    for stage in pipeline.get("stages", []):
        _dispo_stage_cache[stage["name"].lower()] = stage["id"]


def _cached_stage_id(stage_name: str) -> Optional[str]:
    """Return stage ID from cache, loading pipeline if needed."""
    if not _dispo_stage_cache:
        pipeline_id = ensure_dispo_pipeline()
        if not _dispo_stage_cache:
            # Cache not populated by ensure_dispo_pipeline — load separately
            r = _ghl_get("/opportunities/pipelines", params={"locationId": GHL_LOCATION_ID})
            if r and r.status_code == 200:
                for p in r.json().get("pipelines", []):
                    if p["id"] == pipeline_id:
                        _populate_stage_cache(p)
                        break
    return _dispo_stage_cache.get(stage_name.lower())


# ── GHL contact helpers ────────────────────────────────────────────────────────

def find_or_create_ghl_contact(buyer: dict) -> Optional[str]:
    """
    Find a GHL contact by phone. Create one if not found.
    Returns GHL contact_id or None on failure.
    """
    phone = (buyer.get("phone") or "").strip()
    if not phone:
        log.warning("Buyer %s has no phone — cannot create GHL contact", buyer.get("id"))
        return None

    # Search by phone (duplicate check endpoint)
    r = _ghl_get(
        "/contacts/search/duplicate",
        params={"locationId": GHL_LOCATION_ID, "phone": phone},
    )
    if r and r.status_code == 200:
        contact = r.json().get("contact")
        if contact:
            return contact.get("id")

    # Create new contact
    payload = {
        "locationId":  GHL_LOCATION_ID,
        "firstName":   buyer.get("first_name", ""),
        "lastName":    buyer.get("last_name", ""),
        "phone":       phone,
        "email":       buyer.get("email", ""),
        "companyName": buyer.get("company", ""),
        "tags":        ["cash-buyer", "dispo-blast"],
    }
    r = _ghl_post("/contacts/", json=payload)
    if r and r.status_code in (200, 201):
        data = r.json()
        contact_id = (data.get("contact") or {}).get("id") or data.get("id")
        log.info("Created GHL contact for buyer %s: %s", buyer.get("id"), contact_id)
        return contact_id

    log.error(
        "Failed to create GHL contact for buyer %s: %s",
        buyer.get("id"),
        r.text[:200] if r else "no response",
    )
    return None


def _add_note(contact_id: str, note: str) -> None:
    r = _ghl_post(f"/contacts/{contact_id}/notes", json={"body": note, "userId": ""})
    if not (r and r.status_code in (200, 201)):
        log.error("Note failed for contact %s: %s", contact_id, r.text[:200] if r else "no response")


def _send_sms(contact_id: str, message: str) -> bool:
    """Send SMS from JENNI_PHONE to a GHL contact."""
    payload = {
        "type":      "SMS",
        "contactId": contact_id,
        "message":   message,
    }
    if JENNI_PHONE:
        payload["fromNumber"] = JENNI_PHONE
    r = _ghl_post("/conversations/messages", json=payload)
    return r is not None and r.status_code in (200, 201)


# ── GHL opportunity helpers ────────────────────────────────────────────────────

def create_dispo_opp(contact_id: str, deal_id: str, deal_data: dict) -> Optional[str]:
    """Create a GHL opportunity in the Commercial Dispo pipeline at 'Blast Sent'."""
    pipeline_id = ensure_dispo_pipeline()
    stage_id = _cached_stage_id("blast sent")
    if not stage_id:
        log.error("'Blast Sent' stage not found in dispo pipeline")
        return None

    payload = {
        "pipelineId":      pipeline_id,
        "pipelineStageId": stage_id,
        "locationId":      GHL_LOCATION_ID,
        "contactId":       contact_id,
        "name":            f"Dispo — {deal_data.get('address', deal_id)}",
        "monetaryValue":   deal_data.get("asking_price", 0),
        "status":          "open",
    }
    r = _ghl_post("/opportunities/", json=payload)
    if r and r.status_code in (200, 201):
        data = r.json()
        opp_id = (data.get("opportunity") or {}).get("id") or data.get("id")
        log.info("Created dispo opp %s (contact=%s deal=%s)", opp_id, contact_id, deal_id)
        return opp_id

    log.error("Failed to create dispo opp: %s", r.text[:200] if r else "no response")
    return None


def advance_dispo_opp(opp_id: str, stage_name: str) -> bool:
    """Move a dispo opportunity to the given stage."""
    stage_id = _cached_stage_id(stage_name)
    if not stage_id:
        log.error("Stage '%s' not found in dispo pipeline", stage_name)
        return False
    r = _ghl_put(f"/opportunities/{opp_id}", json={"pipelineStageId": stage_id})
    return r is not None and r.status_code in (200, 201)


def find_dispo_opp(contact_id: str, stage_name: Optional[str] = None) -> Optional[str]:
    """
    Find the most recent dispo opportunity for a contact.
    If stage_name is given, only return opps in that stage.
    Returns the GHL opportunity ID or None.
    """
    pipeline_id = ensure_dispo_pipeline()
    r = _ghl_get(
        "/opportunities/search",
        params={
            "location_id": GHL_LOCATION_ID,
            "contact_id":  contact_id,
            "pipeline_id": pipeline_id,
        },
    )
    if not r or r.status_code != 200:
        return None

    opps = r.json().get("opportunities", [])
    if not opps:
        return None

    if stage_name:
        target_stage_id = _cached_stage_id(stage_name)
        opps = [o for o in opps if o.get("pipelineStageId") == target_stage_id]

    return opps[0]["id"] if opps else None


# ── Deal data fetcher ─────────────────────────────────────────────────────────

def get_deal_data(opp_id: str) -> dict:
    """
    Fetch deal details from a GHL opportunity.
    Custom field IDs used: city, state, property_type, cap_rate, noi, unit_count.
    Falls back to empty strings for missing fields.
    """
    r = _ghl_get(f"/opportunities/{opp_id}")
    if not r or r.status_code != 200:
        log.warning("Could not fetch deal data for opp %s", opp_id)
        return {}

    opp = r.json().get("opportunity", r.json()) or {}
    cf = {
        (f.get("fieldKey") or f.get("id") or ""): f.get("fieldValue")
        for f in opp.get("customFields", [])
    }

    asking = float(opp.get("monetaryValue") or 0)
    return {
        "address":              opp.get("name", ""),
        "city":                 cf.get("city", ""),
        "state":                cf.get("state", ""),
        "asking_price":         asking,
        "asking_price_formatted": _fmt_price(asking),
        "property_type":        cf.get("property_type", "commercial property"),
        "cap_rate":             cf.get("cap_rate", "not listed"),
        "noi":                  cf.get("noi", "not listed"),
        "unit_count":           cf.get("unit_count", "N/A"),
    }


# ── SMS blast ─────────────────────────────────────────────────────────────────

def _format_blast_sms(buyer: dict, deal_data: dict) -> str:
    return BLAST_SMS_TEMPLATE.format(
        first_name    = buyer.get("first_name") or "there",
        property_type = deal_data.get("property_type") or "commercial property",
        city          = deal_data.get("city") or "",
        state         = deal_data.get("state") or "",
        unit_count    = deal_data.get("unit_count") or "N/A",
        cap_rate      = deal_data.get("cap_rate") or "not listed",
        asking_price  = deal_data.get("asking_price_formatted") or str(deal_data.get("asking_price", "")),
    )


def blast_buyers(deal_id: str, deal_data: dict, buyers: list) -> int:
    """
    Blast matched buyers with SMS and create GHL dispo opps.

    For each buyer:
      1. Idempotency check — skip if dispo_blasts row exists for (deal_id, buyer_id)
      2. Find or create GHL contact
      3. Send blast SMS (retry once on failure)
      4. Create GHL dispo opp at "Blast Sent"
      5. Insert dispo_blasts row

    Returns count of buyers successfully blasted.
    """
    sb = _get_sb()
    blasted = 0

    for buyer in buyers:
        buyer_id = str(buyer["id"])

        # Idempotency check
        existing = (
            sb.table("dispo_blasts")
            .select("id")
            .eq("deal_opportunity_id", deal_id)
            .eq("buyer_id", buyer_id)
            .execute()
            .data
        )
        if existing:
            log.debug("Already blasted buyer %s on deal %s — skipping", buyer_id, deal_id)
            continue

        contact_id = find_or_create_ghl_contact(buyer)
        if not contact_id:
            log.warning("No GHL contact for buyer %s — skipping blast", buyer_id)
            continue

        sms_text = _format_blast_sms(buyer, deal_data)
        sms_ok = _send_sms(contact_id, sms_text) or _send_sms(contact_id, sms_text)  # retry once

        if not sms_ok:
            log.error(
                "SMS blast failed for buyer %s (contact %s) deal %s",
                buyer_id, contact_id, deal_id,
            )
            # Still create opp to track the attempt
            opp_id = create_dispo_opp(contact_id, deal_id, deal_data)
            if opp_id:
                advance_dispo_opp(opp_id, "Blast Sent")
            continue

        opp_id = create_dispo_opp(contact_id, deal_id, deal_data)

        try:
            sb.table("dispo_blasts").insert({
                "deal_opportunity_id": deal_id,
                "buyer_id":            buyer_id,
                "ghl_contact_id":      contact_id,
                "ghl_opp_id":          opp_id,
            }).execute()
        except Exception as exc:
            log.error(
                "dispo_blasts insert failed buyer=%s deal=%s: %s",
                buyer_id, deal_id, exc,
            )

        blasted += 1

    return blasted


def match_and_blast(deal_id: str, deal_data: dict, deal_contact_id: str) -> dict:
    """
    Full match-and-blast orchestration:
      1. Query matching active buyers from Supabase
      2. Blast all matched buyers
      3. Add summary note to the deal contact in GHL

    Raises RuntimeError if Supabase is unreachable (caller should surface this).
    """
    try:
        buyers = match_buyers(deal_data)
    except Exception as exc:
        log.error("Supabase unreachable in match_and_blast: %s", exc)
        _add_note(
            deal_contact_id,
            "⚠️ Dispo blast ABORTED — Supabase unreachable. Check DB connection and retry.",
        )
        raise

    if not buyers:
        log.warning("match_and_blast: no buyers matched deal %s", deal_id)
        _add_note(
            deal_contact_id,
            "ℹ️ Dispo blast: no buyers matched in cash_buyers DB. "
            "Check buyer criteria (price range, state, property type) and re-run.",
        )
        return {"matched": 0, "blasted": 0}

    blasted = blast_buyers(deal_id, deal_data, buyers)

    _add_note(
        deal_contact_id,
        f"📤 Dispo blast complete — {blasted}/{len(buyers)} buyers contacted for deal {deal_id}.",
    )
    return {"matched": len(buyers), "blasted": blasted}


# ── Inbound reply handler ─────────────────────────────────────────────────────

# Module-level reference — allows tests to patch dispo_tracks.trigger_jenni_call
trigger_jenni_call = None


def _get_trigger_jenni_call():
    """Lazy import so dispo_tracks doesn't circularly depend on jenni_tracks at import time."""
    global trigger_jenni_call
    if trigger_jenni_call is None:
        from jenni_tracks import trigger_jenni_call as _fn
        trigger_jenni_call = _fn
    return trigger_jenni_call


def handle_dispo_reply(contact_id: str, message: str, deal_id: str) -> dict:
    """
    Handle a buyer's SMS reply to a dispo blast.

    Steps:
      1. Classify reply sentiment
      2. Find the buyer's dispo opportunity
      3. Update dispo_blasts with the response
      4. Negative → advance opp to Dead
      5. Positive/unclear → advance opp to Interest Confirmed → Jenni Qualifying,
         then trigger a Jenni outbound call
    """
    sentiment = classify_reply(message)
    opp_id = find_dispo_opp(contact_id)
    deal_data = get_deal_data(deal_id)

    # Record response in dispo_blasts and trigger grade recalc
    try:
        sb = _get_sb()
        updated = (
            sb.table("dispo_blasts")
            .update({"response": message, "outcome": sentiment})
            .eq("deal_opportunity_id", deal_id)
            .eq("ghl_contact_id", contact_id)
            .select("buyer_id")
            .execute()
            .data
        )
        # Recalc buyer grade now that we have a new outcome
        if updated:
            buyer_id = updated[0].get("buyer_id")
            if buyer_id:
                from scripts.buyer_grader import recalc_buyer_grade
                recalc_buyer_grade(str(buyer_id), sb)
    except Exception as exc:
        log.warning("Could not update dispo_blasts response contact=%s deal=%s: %s", contact_id, deal_id, exc)

    if sentiment == "negative":
        if opp_id:
            advance_dispo_opp(opp_id, "Dead")
        return {"sentiment": "negative", "action": "opp_closed"}

    # Fetch contact info once — used by both unclear and positive paths
    contact_r = _ghl_get(f"/contacts/{contact_id}")
    buyer_phone = ""
    buyer_name = "there"
    if contact_r and contact_r.status_code == 200:
        c = contact_r.json().get("contact", contact_r.json()) or {}
        buyer_phone = c.get("phone", "")
        buyer_name = c.get("firstName", "there")

    # Unclear — buyer is engaged but hasn't expressed interest yet.
    # Send a clarification SMS to re-explain the deal; do NOT call Jenni.
    if sentiment == "unclear":
        _addr = deal_data.get("address") or "a property we're working on"
        clarification = (
            f"Hey {buyer_name}! This is Jenni from Helpful Homebuyers. "
            f"I reached out because we have a commercial deal at {_addr} "
            f"that might be a great fit for your buy criteria. "
            f"Are you open to hearing more details? Just reply YES and I'll send them over."
        )
        _send_sms(contact_id, clarification)
        return {"sentiment": "unclear", "action": "clarification_sent"}

    # Positive — advance pipeline and trigger Jenni qualifying call
    if opp_id:
        advance_dispo_opp(opp_id, "Interest Confirmed")
        advance_dispo_opp(opp_id, "Jenni Qualifying")

    _call_fn = _get_trigger_jenni_call()
    call_ok = _call_fn(
        contact_id    = contact_id,
        broker_name   = buyer_name,
        address       = deal_data.get("address", ""),
        to_number     = buyer_phone,
        asking_price  = str(deal_data.get("asking_price", "")),
        property_type = deal_data.get("property_type", ""),
        cap_rate      = deal_data.get("cap_rate", "not listed"),
        noi           = deal_data.get("noi", "not listed"),
        unit_count    = deal_data.get("unit_count", "N/A"),
        context_note  = "dispo_call — buyer replied to blast",
    )

    return {"sentiment": sentiment, "action": "call_triggered", "call_ok": call_ok}


# ── Post-qualification booking ────────────────────────────────────────────────

def handle_buyer_qualified(contact_id: str) -> dict:
    """
    Called after Retell reports buyer_qualified for a Jenni dispo call.

    Steps:
      1. Find dispo opp in 'Jenni Qualifying' stage
      2. Book next available slot on Commercial Deals calendar
      3. Advance opp to 'Call Scheduled'
    """
    opp_id = find_dispo_opp(contact_id, stage_name="Jenni Qualifying")
    appt_id = _book_next_available_slot(contact_id)

    if opp_id:
        advance_dispo_opp(opp_id, "Call Scheduled")
        log.info("Dispo opp %s advanced to Call Scheduled (contact=%s)", opp_id, contact_id)
    else:
        log.warning("handle_buyer_qualified: no Jenni Qualifying opp found for %s", contact_id)

    return {"opp_id": opp_id, "appt_id": appt_id, "booked": appt_id is not None}


def _book_next_available_slot(contact_id: str) -> Optional[str]:
    """
    Find the first available slot on COMMERCIAL_DEALS_CALENDAR_ID tomorrow and book it.
    Returns the GHL appointment ID or None on failure.
    """
    from datetime import datetime, timedelta, timezone

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    date_str = tomorrow.strftime("%Y-%m-%d")
    day_start_ms = int(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    day_end_ms   = int(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

    r = _ghl_post(
        f"/calendars/{COMMERCIAL_DEALS_CALENDAR_ID}/free-slots",
        json={
            "startDate": day_start_ms,
            "endDate":   day_end_ms,
            "timezone":  "America/Los_Angeles",
        },
    )
    if not r or r.status_code != 200:
        log.warning("Could not fetch free slots for Commercial Deals calendar")
        return None

    # Find first available slot
    slot_ms = None
    for _date, day_data in (r.json().get("_dates_") or {}).items():
        if isinstance(day_data, list):
            for group in day_data:
                slots = group.get("slots") or []
                if slots:
                    slot_ms = slots[0]
                    break
        if slot_ms:
            break

    if not slot_ms:
        log.warning("No free slots found on Commercial Deals calendar for %s", date_str)
        return None

    start_dt = datetime.fromtimestamp(slot_ms / 1000, tz=timezone.utc)
    end_dt   = start_dt + timedelta(minutes=APPT_DURATION_MIN)

    payload = {
        "calendarId":        COMMERCIAL_DEALS_CALENDAR_ID,
        "locationId":        GHL_LOCATION_ID,
        "contactId":         contact_id,
        "startTime":         start_dt.isoformat(),
        "endTime":           end_dt.isoformat(),
        "title":             "Commercial Deal Review — Qualified Buyer",
        "appointmentStatus": "new",
        "ignoreDateRange":   False,
        "toNotify":          True,
    }
    r2 = _ghl_post("/calendars/events/appointments", json=payload)
    if r2 and r2.status_code in (200, 201):
        appt = r2.json().get("appointment", r2.json())
        appt_id = appt.get("id")
        log.info("Booked Commercial Deals appt %s for contact %s", appt_id, contact_id)
        return appt_id

    log.error("Failed to book Commercial Deals appt: %s", r2.text[:200] if r2 else "no response")
    return None
