"""
Helpful Home Buyers USA — Webhook Server v3
Handles all 4 Retell agents + full AI follow-up sequence system.

Routes:
  POST /{agent}-check-calendar       (agent = shelby|alex|cole|jordan|jenni)
  POST /{agent}-book-appointment
  POST /{agent}-send-sms
  POST /retell-call-outcome          ← full pipeline: stage + SMS + tags
  POST /retell-call-started          (optional — log call start)
  GET  /health
"""

import os
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from offer_engine.analyze_deal import analyze_deal as _analyze_deal
from fastapi.responses import FileResponse, JSONResponse
import pathlib
from compliance import ComplianceGate, ComplianceBlock, ActionType

# ── Config ────────────────────────────────────────────────────────────────────
GHL_API_KEY     = os.getenv("GHL_API_KEY", "")
ADMIN_KEY       = os.getenv("ADMIN_KEY", "")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
CALENDAR_ID     = os.getenv("CALENDAR_ID", "2xJXutj4eTskFPYx8AeL")
GHL_BASE        = "https://services.leadconnectorhq.com"

# HHB On Market — Residential MLS (Claire)
GHL_LOCATION_ID_ON_MARKET = "18Qc6ZWft7zdNY4oZUSm"
MARCUS_AGENT_ID           = "agent_66939b0a2da6f2e37fe99edc54"

# Jenni Commercial On Market — Crexi/DealSauce (Jenni)
GHL_LOCATION_ID_COMMERCIAL = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
JENNI_AGENT_ID             = os.getenv("JENNI_AGENT_ID", "")
JENNI_PHONE                = os.getenv("JENNI_PHONE", "")
JENNI_CALENDAR_ID          = os.getenv("JENNI_CALENDAR_ID", "")
JENNI_PIPELINE_ID          = os.getenv("JENNI_PIPELINE_ID", "")

# Reagan Surplus Funds — CR_CashRights (Reagan)
REAGAN_AGENT_ID   = "agent_5c5a513db86a21993f8c148ac6"
REAGAN_PHONE      = "+17078463387"

# Appointment duration in minutes (real estate consultations = 30 min min)
APPT_DURATION_MIN = int(os.getenv("APPT_DURATION_MIN", "30"))

GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}

# Map Retell call outcomes → GHL pipeline stage names
from stage_map import STAGE_MAP
from calendar_routing import resolve_calendar_route, routing_summary

# ── Compliance gate ───────────────────────────────────────────────────────────
try:
    from supabase import create_client as _create_client  # type: ignore
    _SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    _SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    _sb = _create_client(_SUPABASE_URL, _SUPABASE_KEY) if _SUPABASE_URL else None
except Exception:
    _sb = None
compliance_gate = ComplianceGate(supabase_client=_sb)

# Flags/urgency values that trigger escalation
URGENT_FLAGS = {"urgent_under_14_days", "critical_-_under_14_days"}

VALID_AGENTS = {"shelby", "alex", "cole", "jordan", "marcus", "jenni"}

# ── Retell agent_id → GHL outbound phone number ───────────────────────────────
AGENT_PHONE_MAP = {
    "agent_bde1f8ca91b3a63a42ecad9777": "+17036915670",  # Shelby inbound
    "agent_40da2f733e42df807a89c669d6": "+17036915670",  # Shelby outbound
    "agent_636dd8ac10f4b633ab38bb001e": "+17038402238",  # Harper bankruptcy
    "agent_e6cafef912272207148d11893f": "+17038402238",  # Harper bankruptcy alt
    "agent_56e1def11bd5201bcdc1fedd6b": "+12133720548",  # Riley acquisitions
    "agent_dd0928ae5479516c905c55ca4d": "+12134747691",  # Brooke estate
    MARCUS_AGENT_ID: os.getenv("MARCUS_PHONE", ""),      # Claire MLS On Market (set MARCUS_PHONE env var)
    JENNI_AGENT_ID:  JENNI_PHONE,                         # Jenni Commercial On Market
    REAGAN_AGENT_ID: REAGAN_PHONE,                        # Reagan Surplus Funds
}

# Agent name (URL param) → outbound GHL phone number for SMS
AGENT_NAME_PHONE_MAP: dict[str, str] = {
    "shelby":  "+17036915670",
    "alex":    "+17038402238",
    "cole":    "+12133720548",
    "jordan":  "+12134747691",
    "marcus":  os.getenv("MARCUS_PHONE", ""),
    "jenni":   JENNI_PHONE,
}

# ── Outcome-based SMS templates (NEPQ/Hormozi style) ─────────────────────────
# {name} = contact first name, {address} = property address
SMS_TEMPLATES: dict[str, Optional[str]] = {
    "Appointment Set": (
        "Hey {name}! Shelby from Helpful Home Buyers USA. You're all set ✅ "
        "We'll go over your options for {address} and put together a real cash number for you. "
        "If anything changes, just reply here. Talk soon!"
    ),
    "Needs Human Offer Review": (
        "Hey {name} — thanks for talking with us about {address}. "
        "We have enough to get this in front of the team for a real review. "
        "We'll circle back shortly with the cleanest next step."
    ),
    "Short Sale Review": (
        "Hey {name} — we’re moving your file for {address} into short sale review now. "
        "Our next step is to line up the lender/workout details and come back with the cleanest path. "
        "If you get any bank notices, text them here."
    ),
    "Cash Offer Ready": (
        "Hey {name} — we’ve got what we need on {address} to put together the cash path. "
        "The team is reviewing it now and we’ll follow up with numbers shortly."
    ),
    "Novation Review": (
        "Hey {name} — we’re reviewing the best retail-style exit for {address} now. "
        "That means looking at how to preserve the most value without creating extra work for you. "
        "We’ll come back with the cleanest option."
    ),
    "Attorney Intro Agreed": (
        "Hey {name}, Shelby from Helpful Home Buyers USA. "
        "Really appreciate you chatting today. We'll get our attorney connected with yours — "
        "we handle everything on our end. Any questions, just reply here."
    ),
    "Seeds Planted": (
        "Hey {name}, Shelby from Helpful Home Buyers USA. "
        "Quick question — what would getting a fair cash offer on {address} this week actually mean for your situation? "
        "No repairs, no fees, we close on your timeline. Just reply and let me know."
    ),
    "Micro-Commitment": (
        "Hey {name} — Shelby here with Helpful Home Buyers USA. "
        "Really glad we connected. We can move fast on {address} when you're ready. "
        "What's one thing that would need to happen for this to make sense for you? Just reply."
    ),
    "Interested - Reviewing": (
        "Hey {name}, this is Shelby from Helpful Home Buyers USA. "
        "Take your time — we're not going anywhere. "
        "When you're ready to talk numbers on {address}, just reply and I'll get a cash offer to you same day."
    ),
    "Call Back Later": (
        "Hey {name}, Shelby from Helpful Home Buyers USA. No worries at all — "
        "whenever the timing feels right, just reply here and we'll get moving. "
        "We close fast and handle everything."
    ),
    "Not Ready": (
        "Hey {name}, this is Shelby from Helpful Home Buyers USA. "
        "Totally understand — no rush. When things change with {address}, "
        "just reply and we'll be ready. We can close in as little as 7 days when you are."
    ),
    "Voicemail": (
        "Hey {name}, this is Shelby from Helpful Home Buyers USA. "
        "Left you a voicemail about {address}. We buy houses as-is for cash — no repairs, no fees. "
        "When's a good time to connect? Just reply here."
    ),
    "No Answer": (
        "Hey {name}, Shelby from Helpful Home Buyers USA here. "
        "Tried reaching you today about {address}. "
        "We buy homes as-is for cash and close fast. Is this still a good number? Reply YES."
    ),
    # Dead outcomes — no SMS
    "Not Interested":         None,
    "Disqualified":           None,
    "DQ - Not Heir":          None,
    "DQ - Already Sold":      None,
    "DQ - Active Litigation": None,
    "Wrong Number":           None,
    "Disconnected":           None,
}

# ── GHL tags per outcome — trigger email drip workflows in GHL ────────────────
OUTCOME_TAGS: dict[str, list[str]] = {
    "Appointment Set":        ["ai-followup-appointment", "ai-hot-lead"],
    "Attorney Intro Agreed":  ["ai-followup-appointment", "ai-hot-lead"],
    "Needs Human Offer Review": ["ai-offer-review", "ai-warm-lead"],
    "Short Sale Review":        ["ai-short-sale-review", "ai-warm-lead"],
    "Cash Offer Ready":         ["ai-cash-offer", "ai-hot-lead"],
    "Novation Review":          ["ai-novation-review", "ai-warm-lead"],
    "Seeds Planted":          ["ai-followup-hot", "ai-warm-lead"],
    "Micro-Commitment":       ["ai-followup-hot", "ai-warm-lead"],
    "Interested - Reviewing": ["ai-followup-hot", "ai-warm-lead"],
    "Call Back Later":        ["ai-followup-callback"],
    "Not Ready":              ["ai-followup-nurture"],
    "Voicemail":              ["ai-followup-no-answer"],
    "No Answer":              ["ai-followup-no-answer"],
    "Not Interested":         ["ai-dead-not-interested"],
    "Disqualified":           ["ai-dead-dq"],
    "DQ - Not Heir":          ["ai-dead-dq"],
    "DQ - Already Sold":      ["ai-dead-dq"],
    "DQ - Active Litigation": ["ai-dead-dq"],
    "Wrong Number":           ["ai-dead-dq"],
    "Disconnected":           ["ai-dead-dq"],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Helpful Home Buyers USA Webhooks", version="2.0.0")

_STATIC_DIR = pathlib.Path(__file__).parent

from deal_updater import router as deal_router
app.include_router(deal_router)

# ── Pipeline stage cache ──────────────────────────────────────────────────────
_pipeline_cache: dict = {}

def _load_pipeline_cache():
    r = _ghl_get("/opportunities/pipelines", params={"locationId": GHL_LOCATION_ID})
    if r and r.status_code == 200:
        for p in r.json().get("pipelines", []):
            _pipeline_cache[p["id"]] = {s["name"].lower(): s["id"] for s in p.get("stages", [])}
        log.info("Pipeline cache: %d pipelines loaded", len(_pipeline_cache))

def _get_stage_id(pipeline_id: str, stage_name: str) -> Optional[str]:
    if not _pipeline_cache:
        _load_pipeline_cache()
    return _pipeline_cache.get(pipeline_id, {}).get(stage_name.lower())

# ── GHL API with retry ────────────────────────────────────────────────────────

def _ghl_request(method: str, path: str, retries: int = 3, **kwargs) -> Optional[requests.Response]:
    url = f"{GHL_BASE}{path}"
    kwargs.setdefault("headers", GHL_HEADERS)
    kwargs.setdefault("timeout", 15)
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code == 429:
                _raw_ra = r.headers.get("Retry-After", "")
                wait = int(_raw_ra) if _raw_ra.isdigit() else 2 ** attempt
                log.warning("GHL rate limit on %s, retry in %ds", path, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < retries - 1:
                time.sleep(1)
                continue
            return r
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                log.error("GHL %s %s failed after %d attempts: %s", method, path, retries, e)
    return None

def _ghl_get(path, **kw):  return _ghl_request("GET",  path, **kw)
def _ghl_post(path, **kw): return _ghl_request("POST", path, **kw)
def _ghl_put(path, **kw):  return _ghl_request("PUT",  path, **kw)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _day_timestamps(date_str: str):
    day_start = datetime.fromisoformat(f"{date_str}T00:00:00").replace(tzinfo=timezone.utc)
    day_end   = datetime.fromisoformat(f"{date_str}T23:59:59").replace(tzinfo=timezone.utc)
    return int(day_start.timestamp() * 1000), int(day_end.timestamp() * 1000)

def _format_slots(raw: dict, tz: str = "America/New_York") -> list:
    available = []
    for _date, day_data in (raw.get("_dates_") or {}).items():
        if not isinstance(day_data, list):
            continue
        for group in day_data:
            for slot_ms in (group.get("slots") or []):
                dt = datetime.fromtimestamp(slot_ms / 1000, tz=timezone.utc)
                available.append({
                    "datetime_iso": dt.isoformat(),
                    "time_display": dt.strftime("%-I:%M %p"),
                    "timestamp_ms": slot_ms,
                })
    return available

def _verify_contact(contact_id: str) -> bool:
    r = _ghl_get(f"/contacts/{contact_id}")
    return r is not None and r.status_code == 200

def _add_note(contact_id: str, note: str) -> Optional[str]:
    r = _ghl_post(f"/contacts/{contact_id}/notes", json={"body": note, "userId": ""})
    if r and r.status_code in (200, 201):
        return r.json().get("id")
    log.error("Note failed for contact %s: %s", contact_id, r.text[:200] if r else "no response")
    return None

def _update_email(contact_id: str, email: str):
    if not email or "@" not in email:
        return
    r = _ghl_put(f"/contacts/{contact_id}", json={"email": email})
    if r and r.status_code in (200, 201):
        log.info("Email updated on contact %s → %s", contact_id, email)

def _update_stage(opp_id: str, pipeline_id: str, stage_name: str) -> bool:
    stage_id = _get_stage_id(pipeline_id, stage_name)
    if not stage_id:
        log.warning("Stage '%s' not found in pipeline %s", stage_name, pipeline_id)
        return False
    r = _ghl_put(f"/opportunities/{opp_id}", json={"pipelineStageId": stage_id})
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("Opportunity %s → '%s'", opp_id, stage_name)
    return success

# ── Follow-up helpers ─────────────────────────────────────────────────────────

def _get_contact_first_name(contact_id: str) -> str:
    """Fetch contact first name from GHL for SMS personalization."""
    r = _ghl_get(f"/contacts/{contact_id}")
    if r and r.status_code == 200:
        contact = r.json().get("contact", r.json())
        return (contact.get("firstName") or contact.get("first_name") or "").strip() or "there"
    return "there"

def _get_contact_address(contact_id: str) -> str:
    """Fetch contact address1 from GHL for SMS personalization. Returns '' on any error."""
    r = _ghl_get(f"/contacts/{contact_id}")
    if r and r.status_code == 200:
        contact = r.json().get("contact", r.json())
        return (contact.get("address1") or "").strip()
    return ""

def _send_followup_sms(contact_id: str, outcome: str, name: str, address: str, from_number: str) -> bool:
    """Send outcome-based follow-up SMS via GHL immediately after call ends."""
    template = SMS_TEMPLATES.get(outcome)
    if not template:
        log.info("No SMS template for outcome '%s' — skipping", outcome)
        return False

    first_name = name.split()[0] if name and name not in ("there", "") else "there"
    address_display = address.strip() if address else "your property"
    message = template.format(name=first_name, address=address_display)

    payload: dict = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
    }
    if from_number:
        payload["fromNumber"] = from_number

    r = _ghl_post("/conversations/messages", json=payload)
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("Follow-up SMS sent → contact %s [outcome: %s]", contact_id, outcome)
    else:
        log.error("Follow-up SMS FAILED contact=%s status=%s body=%s",
                  contact_id, r.status_code if r else "none", r.text[:200] if r else "")
    return success

def _apply_outcome_tags(contact_id: str, outcome: str) -> bool:
    """Apply GHL tags based on outcome — triggers email drip workflows configured in GHL."""
    tags = OUTCOME_TAGS.get(outcome, [])
    if not tags:
        return False
    r = _ghl_post(f"/contacts/{contact_id}/tags", json={"tags": tags})
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("Tags applied to contact %s: %s", contact_id, tags)
    else:
        log.error("Tag apply failed contact=%s: %s", contact_id, r.text[:100] if r else "no response")
    return success


def fetch_contact(contact_id: str) -> dict:
    """Fetch a contact from GHL by ID."""
    r = requests.get(
        f"{GHL_BASE}/contacts/{contact_id}",
        headers=GHL_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("contact", {})


def compliance_check(contact_id: str, action: ActionType) -> None:
    """Fetch contact and run compliance gate. Raises ComplianceBlock if blocked."""
    contact = fetch_contact(contact_id)
    compliance_gate.check(contact, action)

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    log.info("Helpful Home Buyers USA Webhook Server v3 starting")
    _load_pipeline_cache()
    from mls_tracks import start_scheduler
    start_scheduler()
    from jenni_tracks import start_scheduler as jenni_start_scheduler
    jenni_start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    from mls_tracks import stop_scheduler
    stop_scheduler()
    from jenni_tracks import stop_scheduler as jenni_stop_scheduler
    jenni_stop_scheduler()

# ── Health ────────────────────────────────────────────────────────────────────

def _get_sb_for_health():
    """Probe Supabase by selecting a row from cash_buyers. Raises on any error."""
    try:
        from dispo_tracks import _get_sb
        sb = _get_sb()
        sb.table("cash_buyers").select("id").limit(1).execute()
    except ImportError:
        pass  # Supabase not configured — skip check


@app.post("/refresh-buyers")
async def refresh_buyers(request: Request):
    """
    Trigger a DealSauce buyer list refresh. Protected by X-Admin-Key header.
    Runs scrape_all_buyers() then upserts results to Supabase cash_buyers.
    """
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or provided_key != ADMIN_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from scripts.dealsauce_scraper import scrape_all_buyers, upsert_buyers
    try:
        buyers = await scrape_all_buyers()
        inserted, updated = upsert_buyers(buyers)
        log.info("/refresh-buyers: inserted=%d updated=%d", inserted, updated)
        return {"inserted": inserted, "updated": updated, "total": len(buyers)}
    except Exception as exc:
        log.error("/refresh-buyers failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
def health():
    checks: dict[str, str] = {}

    # GHL liveness — direct call, no retries, 3s timeout
    try:
        _r = requests.get(
            f"{GHL_BASE}/locations/{GHL_LOCATION_ID}",
            headers=GHL_HEADERS,
            timeout=3,
        )
        checks["ghl"] = "ok" if _r.status_code == 200 else f"error: status {_r.status_code}"
    except Exception as _exc:
        checks["ghl"] = f"error: {_exc}"

    # Supabase liveness
    try:
        _get_sb_for_health()
        checks["supabase"] = "ok"
    except Exception as exc:
        checks["supabase"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    body = {
        "status":                 "ok" if all_ok else "degraded",
        "version":                "2.0.0",
        "checks":                 checks,
        "pipeline_stages_cached": sum(len(v) for v in _pipeline_cache.values()),
        "appt_duration_min":      APPT_DURATION_MIN,
        "calendar_routing":       routing_summary(),
    }

    if not all_ok:
        return JSONResponse(body, status_code=503)
    return body

# ── Calendar availability ─────────────────────────────────────────────────────

@app.post("/{agent}-check-calendar")
async def check_calendar(agent: str, request: Request):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail="Unknown agent")

    body = await request.json()
    date = body.get("date")
    tz   = body.get("timezone", "America/New_York")

    if not date:
        return JSONResponse({"error": "Missing: date"}, status_code=400)

    try:
        route = resolve_calendar_route(
            agent_name=agent,
            contact_id=body.get("contact_id"),
            requested_calendar_id=body.get("calendar_id"),
            requested_owner_key=body.get("routing_owner_key"),
            mode_override=body.get("routing_mode"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    start_ms, end_ms = _day_timestamps(date)
    r = _ghl_get(
        f"/calendars/{route.calendar_id}/free-slots",
        params={"startDate": start_ms, "endDate": end_ms, "timezone": tz},
    )

    if not r or r.status_code != 200:
        log.error("[%s] check-calendar failed: %s", agent, r.status_code if r else "no response")
        return JSONResponse({"error": "calendar unavailable"}, status_code=502)

    slots = _format_slots(r.json(), tz)
    log.info(
        "[%s] check-calendar %s → %d slots via %s (%s)",
        agent,
        date,
        len(slots),
        route.owner_name,
        route.calendar_id,
    )
    return {
        "available_slots": slots,
        "count": len(slots),
        "date": date,
        "calendar_id": route.calendar_id,
        "calendar_owner_key": route.owner_key,
        "calendar_owner_name": route.owner_name,
        "routing_mode": route.mode,
        "routing_reason": route.selection_reason,
    }

# ── Book appointment ──────────────────────────────────────────────────────────

@app.post("/{agent}-book-appointment")
async def book_appointment(agent: str, request: Request):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail="Unknown agent")

    body       = await request.json()
    contact_id = body.get("contact_id")
    start_time = body.get("start_time")
    title      = body.get("title", "Helpful Home Buyers USA Consultation")
    notes      = body.get("notes", "")

    if not contact_id or not start_time:
        return JSONResponse({"error": "Missing: contact_id, start_time"}, status_code=400)

    # Verify contact exists before booking
    if not _verify_contact(contact_id):
        log.error("[%s] Contact not found: %s", agent, contact_id)
        return JSONResponse({"error": f"Contact {contact_id} not found in GHL"}, status_code=404)

    try:
        route = resolve_calendar_route(
            agent_name=agent,
            contact_id=contact_id,
            requested_calendar_id=body.get("calendar_id"),
            requested_owner_key=body.get("routing_owner_key"),
            mode_override=body.get("routing_mode"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse({"error": f"Invalid start_time: {start_time}"}, status_code=400)

    end_dt = start_dt + timedelta(minutes=APPT_DURATION_MIN)

    payload = {
        "calendarId":        route.calendar_id,
        "locationId":        route.location_id,
        "contactId":         contact_id,
        "startTime":         start_dt.isoformat(),
        "endTime":           end_dt.isoformat(),
        "title":             title,
        "appointmentStatus": "new",
        "ignoreDateRange":   False,
        "toNotify":          True,
    }
    if notes:
        payload["notes"] = notes

    r = _ghl_post("/calendars/events/appointments", json=payload)

    if not r or r.status_code not in (200, 201):
        status = r.status_code if r else 0
        log.error("[%s] booking failed %s: %s", agent, status, r.text[:200] if r else "")
        return JSONResponse({"error": "booking failed", "status": status}, status_code=502)

    appt    = r.json().get("appointment", r.json())
    appt_id = appt.get("id")
    log.info(
        "[%s] booked %s for contact %s (%dmin) via %s (%s)",
        agent,
        appt_id,
        contact_id,
        APPT_DURATION_MIN,
        route.owner_name,
        route.calendar_id,
    )

    # ── Confirmation SMS (single GHL contact fetch for name + address) ────────
    _from_no = AGENT_NAME_PHONE_MAP.get(agent, "")
    if _from_no:
        try:
            _cr = _ghl_get(f"/contacts/{contact_id}")
            if _cr and _cr.status_code == 200:
                _c       = _cr.json().get("contact", _cr.json())
                _name    = (_c.get("firstName") or "").strip() or "there"
                _address = (_c.get("address1") or "").strip()
            else:
                _name, _address = "there", ""
            _send_followup_sms(contact_id, "Appointment Set", _name, _address, _from_no)
        except Exception as _sms_err:
            log.warning("[%s] Appointment Set SMS failed (booking still ok): %s", agent, _sms_err)

    return {
        "success":            True,
        "appointment_id":     appt_id,
        "start_time_display": start_dt.strftime("%A, %B %-d at %-I:%M %p"),
        "duration_minutes":   APPT_DURATION_MIN,
        "calendar_id":        route.calendar_id,
        "calendar_owner_key": route.owner_key,
        "calendar_owner_name": route.owner_name,
        "routing_mode":       route.mode,
    }

# ── Send SMS ──────────────────────────────────────────────────────────────────

@app.post("/{agent}-send-sms")
async def send_sms(agent: str, request: Request):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail="Unknown agent")

    body       = await request.json()
    contact_id = body.get("contact_id")
    message    = body.get("message")

    if not contact_id or not message:
        return JSONResponse({"error": "Missing: contact_id, message"}, status_code=400)

    # Compliance gate — must pass before any outbound SMS
    try:
        compliance_check(contact_id, ActionType.SMS)
    except ComplianceBlock as e:
        logging.warning(f"SMS blocked by compliance: {e}")
        return JSONResponse({"status": "blocked", "reason": str(e)}, status_code=200)

    r = _ghl_post(
        "/conversations/messages",
        json={"type": "SMS", "contactId": contact_id, "message": message},
    )

    if not r or r.status_code not in (200, 201):
        log.error("[%s] SMS failed: %s", agent, r.status_code if r else "no response")
        return JSONResponse({"error": "SMS failed"}, status_code=502)

    data = r.json()
    log.info("[%s] SMS sent → contact %s", agent, contact_id)
    return {"success": True, "message_id": data.get("id") or data.get("messageId")}

# ── Call started (logging) ────────────────────────────────────────────────────

@app.post("/retell-call-started")
async def retell_call_started(request: Request):
    body       = await request.json()
    call_id    = body.get("call_id", "")
    dynamic    = body.get("retell_llm_dynamic_variables") or {}
    contact_id = dynamic.get("contact_id", "")
    agent_id   = body.get("agent_id", "")
    log.info("CALL STARTED call_id=%s contact=%s agent=%s", call_id, contact_id, agent_id)
    return {"received": True}

# ── Retell call outcome ───────────────────────────────────────────────────────

@app.post("/retell-call-outcome")
async def retell_call_outcome(request: Request):
    body = await request.json()

    call_id       = body.get("call_id", "")
    call_analysis = body.get("call_analysis") or {}
    custom        = call_analysis.get("custom_analysis_data") or {}
    dynamic_vars  = body.get("retell_llm_dynamic_variables") or {}

    contact_id    = dynamic_vars.get("contact_id") or custom.get("contact_id", "")
    lead_type     = dynamic_vars.get("lead_type") or custom.get("lead_type", "general")
    call_outcome  = custom.get("call_outcome") or call_analysis.get("call_summary", "") or "No Answer"
    sentiment     = call_analysis.get("user_sentiment", "")
    summary       = call_analysis.get("call_summary", "")
    transcript    = body.get("transcript", "")
    duration_ms   = body.get("duration_ms", 0)
    attempt_count = int(dynamic_vars.get("attempt_count", "0")) + 1

    # Structured fields from post-call analysis
    email_captured   = custom.get("email_captured", "")
    appointment_set  = custom.get("appointment_set", False)
    appt_datetime    = custom.get("appointment_datetime", "")
    motivation_level = custom.get("motivation_level", "")
    flags            = custom.get("flags", "")
    urgency_level    = custom.get("urgency_level", "") or custom.get("urgency", "")
    prop_address     = custom.get("property_address", "")
    prop_condition   = custom.get("property_condition", "")
    offer_range      = custom.get("estimated_offer_range", "")
    caller_name      = custom.get("caller_name", "")
    agent_notes      = custom.get("notes", "")

    log.info(
        "call-outcome call_id=%s contact=%s outcome=%s lead=%s duration=%ds",
        call_id, contact_id, call_outcome, lead_type, round(duration_ms / 1000)
    )

    if not contact_id:
        log.warning("No contact_id for call %s", call_id)
        return JSONResponse({"success": False, "error": "no contact_id"})

    # ── Reagan surplus funds — early return (dedicated pipeline, no general processing) ──
    _agent_id = body.get("agent_id", "")
    if _agent_id == REAGAN_AGENT_ID:
        from surplus_tracks import handle_call_outcome as surplus_handle
        surplus_result = surplus_handle(body)
        log.info(
            "Reagan surplus outcome: contact=%s outcome=%s stage_moved=%s",
            surplus_result.get("contact_id"),
            surplus_result.get("outcome"),
            surplus_result.get("stage_moved"),
        )
        return {
            "success":      surplus_result.get("success", True),
            "contact_id":   surplus_result.get("contact_id"),
            "outcome":      surplus_result.get("outcome"),
            "opp_id":       surplus_result.get("opp_id"),
            "stage_moved":  surplus_result.get("stage_moved"),
            "tag_applied":  surplus_result.get("tag_applied"),
            "task_added":   surplus_result.get("task_added"),
            "note_added":   surplus_result.get("note_added"),
            "track":        "surplus",
        }

    ghl_stage = STAGE_MAP.get(call_outcome, "AI - No Answer")

    # ── Build comprehensive note ──────────────────────────────────────────────
    lines = [
        "📞 Retell AI Call Summary",
        f"Call ID: {call_id}",
        f"Outcome: {call_outcome}",
        f"Lead Type: {lead_type}",
        f"Sentiment: {sentiment}",
        f"Duration: {round(duration_ms / 1000)}s",
        f"Attempt #: {attempt_count}",
    ]
    if caller_name:      lines.append(f"Caller: {caller_name}")
    if prop_address:     lines.append(f"Property: {prop_address}")
    if prop_condition:   lines.append(f"Condition: {prop_condition}")
    if offer_range:      lines.append(f"Offer Range: {offer_range}")
    if motivation_level: lines.append(f"Motivation: {motivation_level}/10")
    if flags:            lines.append(f"Flags: {flags}")
    if urgency_level:    lines.append(f"Urgency: {urgency_level}")
    if appointment_set and appt_datetime:
        lines.append(f"Appointment: {appt_datetime}")
    if agent_notes:
        lines.append(f"\nAgent Notes: {agent_notes}")
    if summary:
        lines.append(f"\nAI Summary: {summary}")
    if transcript:
        lines.append(f"\n--- FULL TRANSCRIPT ---\n{transcript}")  # no truncation

    note = "\n".join(lines)

    # ── Write note to GHL ─────────────────────────────────────────────────────
    note_id = _add_note(contact_id, note)

    # ── Update email if captured ──────────────────────────────────────────────
    if email_captured:
        _update_email(contact_id, email_captured)

    # ── Detect urgency ────────────────────────────────────────────────────────
    flag_list = [f.strip().lower() for f in flags.split(",") if f.strip()]
    is_urgent = (
        any(f in URGENT_FLAGS for f in flag_list)
        or "14 days" in urgency_level.lower()
        or "critical" in urgency_level.lower()
    )

    # ── Update opportunity stage ──────────────────────────────────────────────
    opps = []
    r = _ghl_get(
        "/opportunities/search",
        params={"location_id": GHL_LOCATION_ID, "contact_id": contact_id, "limit": 5},
    )
    if r and r.status_code == 200:
        opps = r.json().get("opportunities", [])

    stage_updated = False
    if opps:
        opp = opps[0]
        stage_updated = _update_stage(opp["id"], opp["pipelineId"], ghl_stage)

        # Urgent: add separate escalation alert note
        if is_urgent and stage_updated:
            _add_note(
                contact_id,
                f"🚨 URGENT — IMMEDIATE ACTION NEEDED\n"
                f"Call ID: {call_id}\n"
                f"Urgency: {urgency_level}\n"
                f"Flags: {flags}\n"
                f"Property: {prop_address}\n"
                f"This deal is under 14 days — needs human follow-up today.",
            )
            log.info("Urgent escalation note added for contact %s", contact_id)

    # ── Get contact name for SMS personalization ──────────────────────────────
    contact_name = caller_name or _get_contact_first_name(contact_id)

    # ── Apply outcome tags (triggers GHL email drip workflows) ────────────────
    tags_applied = _apply_outcome_tags(contact_id, call_outcome)

    # ── Send follow-up SMS immediately after call ends ────────────────────────
    from_number = AGENT_PHONE_MAP.get(body.get("agent_id", ""), "")
    # Compliance gate — must pass before any outbound SMS post-call
    try:
        compliance_check(contact_id, ActionType.CALL)
    except ComplianceBlock as e:
        logging.warning(f"Call blocked by compliance: {e}")
        return JSONResponse({"status": "blocked", "reason": str(e)}, status_code=200)
    sms_sent = _send_followup_sms(contact_id, call_outcome, contact_name, prop_address, from_number)

    # ── MLS track dispatch (Marcus calls only) ────────────────────────────────
    track_dispatched = None
    agent_id = body.get("agent_id", "")
    if agent_id == MARCUS_AGENT_ID:
        from mls_tracks import dispatch_track
        contact_phone = ""
        contact_r = _ghl_get(f"/contacts/{contact_id}")
        if contact_r and contact_r.status_code == 200:
            contact_phone = (contact_r.json().get("contact", contact_r.json()) or {}).get("phone", "")
        offer_data = {
            "address": prop_address,
            "offer_price": float(custom.get("offer_price", 0) or 0),
            "arv": float(custom.get("arv", 0) or 0),
            "repair_estimate": float(custom.get("repair_estimate", 0) or 0),
            "list_price": float(custom.get("list_price", 0) or 0),
            "days_on_market": custom.get("days_on_market", ""),
            "agent_name": custom.get("listing_agent_name", ""),
            "agent_phone": custom.get("listing_agent_phone", ""),
            "comps": custom.get("comps", []),
            "contact_name": contact_name,
        }
        track_dispatched = dispatch_track(
            contact_id, call_outcome, contact_name, prop_address, contact_phone,
            offer_data=offer_data if call_outcome in ("interested_write_offer", "Interested - Write Offer") else None,
        )
        if track_dispatched:
            log.info("MLS Track %s dispatched for contact %s", track_dispatched, contact_id)

    # ── Jenni track dispatch (commercial Crexi calls) ─────────────────────────
    jenni_track = None
    if agent_id == JENNI_AGENT_ID:
        from jenni_tracks import dispatch_track as jenni_dispatch
        contact_phone = ""
        contact_r = _ghl_get(f"/contacts/{contact_id}")
        if contact_r and contact_r.status_code == 200:
            contact_phone = (contact_r.json().get("contact", contact_r.json()) or {}).get("phone", "")
        jenni_track = jenni_dispatch(
            contact_id, call_outcome, contact_name, prop_address, contact_phone,
            asking_price=dynamic_vars.get("asking_price", ""),
            property_type=dynamic_vars.get("property_type", ""),
            days_on_market=dynamic_vars.get("days_on_market", ""),
            cap_rate=dynamic_vars.get("cap_rate", "not listed"),
            noi=custom.get("noi_collected") or dynamic_vars.get("noi", "not listed"),
            unit_count=dynamic_vars.get("unit_count", "N/A"),
        )
        if jenni_track:
            log.info("Jenni Track %s dispatched for contact %s", jenni_track, contact_id)

        # ── buyer_qualified → book Commercial Deals calendar ─────────────────
        if call_outcome == "buyer_qualified":
            from dispo_tracks import handle_buyer_qualified
            booking = handle_buyer_qualified(contact_id)
            log.info(
                "buyer_qualified: contact=%s opp=%s booked=%s appt=%s",
                contact_id, booking.get("opp_id"), booking.get("booked"), booking.get("appt_id"),
            )

    return {
        "success":          True,
        "contact_id":       contact_id,
        "outcome":          call_outcome,
        "stage":            ghl_stage,
        "stage_updated":    stage_updated,
        "note_added":       note_id is not None,
        "email_updated":    bool(email_captured),
        "urgent":           is_urgent,
        "sms_sent":         sms_sent,
        "tags_applied":     tags_applied,
        "mls_track":        track_dispatched,
        "jenni_track":      jenni_track,
        "dispo_booked":     locals().get("booking", {}).get("booked"),
    }


# ── GHL inbound SMS webhook (Track B reply → instant Marcus call) ─────────────

@app.post("/ghl-inbound-sms")
async def ghl_inbound_sms(request: Request):
    """
    Called by GHL workflow when a contact sends an inbound SMS.
    If the contact has the mls-track-b-backup tag, cancel the drip
    and trigger an instant Marcus call.

    GHL workflow setup:
      Trigger: Customer Reply (SMS)
      Action: Webhook POST https://<server>/ghl-inbound-sms
      Body: { "contact_id": "{{contact.id}}", "message": "{{message.body}}" }
    """
    body = await request.json()
    contact_id = body.get("contact_id", "")
    message    = body.get("message", "")
    deal_id    = body.get("deal_id", "")   # optional — passed by dispo GHL workflow

    if not contact_id:
        return JSONResponse({"error": "missing contact_id"}, status_code=400)

    contact_r = _ghl_get(f"/contacts/{contact_id}")
    if not contact_r or contact_r.status_code != 200:
        return JSONResponse({"handled": False, "reason": "contact not found"})

    contact = contact_r.json().get("contact", contact_r.json()) or {}
    tags = contact.get("tags", [])

    handled_sources: dict = {}

    # ── Dispo buyer reply ───────────────────────────────────────────────────────
    if "dispo-blast" in tags:
        if not deal_id:
            # Look up deal_id from most recent unanswered dispo_blast for this contact
            from dispo_tracks import _get_sb as _dispo_sb
            try:
                sb = _dispo_sb()
                rows = (
                    sb.table("dispo_blasts")
                    .select("deal_opportunity_id")
                    .eq("ghl_contact_id", contact_id)
                    .is_("response", "null")
                    .order("blasted_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                )
                deal_id = rows[0]["deal_opportunity_id"] if rows else ""
            except Exception as exc:
                log.error("Could not look up deal_id for dispo reply contact=%s: %s", contact_id, exc)

        if deal_id:
            from dispo_tracks import handle_dispo_reply
            result = handle_dispo_reply(contact_id, message, deal_id)
            handled_sources["dispo"] = result
            log.info("Dispo reply handled contact=%s sentiment=%s", contact_id, result.get("sentiment"))
        else:
            log.warning("dispo-blast contact %s replied but no deal_id found", contact_id)

    # ── MLS Track B reply ───────────────────────────────────────────────────────
    if "mls-track-b-backup" in tags:
        from mls_tracks import handle_inbound_reply
        triggered = handle_inbound_reply(contact_id, message)
        handled_sources["mls_track_b"] = {"call_triggered": triggered}
        log.info("Track B reply handler → contact=%s triggered=%s", contact_id, triggered)

    if not handled_sources:
        log.debug("ghl-inbound-sms: contact %s not in Track B or dispo — ignoring", contact_id)
        return JSONResponse({"handled": False, "reason": "not track B or dispo contact"})

    return {"handled": True, **handled_sources}


# ── GHL stage-change webhook → dispo blast trigger ────────────────────────────

UNDER_CONTRACT_STAGE_ID = "7ac4e3fd"


@app.post("/ghl-stage-change")
async def ghl_stage_change(request: Request):
    """
    Fired by a GHL workflow when an opportunity moves to "Under Contract".

    GHL workflow setup:
      Trigger: Opportunity Stage Changed
      Condition: Pipeline Stage = Under Contract (ID: 7ac4e3fd)
      Action: Webhook POST https://<server>/ghl-stage-change
      Body: {
        "opportunity_id": "{{opportunity.id}}",
        "contact_id":     "{{contact.id}}",
        "stage_id":       "{{opportunity.pipelineStageId}}"
      }
    """
    body = await request.json()
    opp_id     = body.get("opportunity_id", "")
    contact_id = body.get("contact_id", "")
    stage_id   = body.get("stage_id", "")

    if not opp_id or not contact_id:
        return JSONResponse({"error": "missing opportunity_id or contact_id"}, status_code=400)

    if stage_id and stage_id != UNDER_CONTRACT_STAGE_ID:
        return JSONResponse({
            "handled": False,
            "reason": f"stage {stage_id} is not Under Contract — ignoring",
        })

    from dispo_tracks import match_and_blast, get_deal_data
    deal_data = get_deal_data(opp_id)

    try:
        result = match_and_blast(opp_id, deal_data, contact_id)
    except Exception as exc:
        log.error("/ghl-stage-change blast failed opp=%s: %s", opp_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    log.info(
        "Dispo blast triggered opp=%s matched=%s blasted=%s",
        opp_id, result.get("matched"), result.get("blasted"),
    )
    return {"handled": True, "opportunity_id": opp_id, **result}


# ── Manual offer+comp email trigger ──────────────────────────────────────────────

@app.post("/send-offer-comp-email")
async def send_offer_comp_email_route(request: Request):
    """
    Manually trigger the Offer + Comp Package email for a contact.

    Body:
      {
        "contact_id": "...",
        "address": "123 Main St",
        "offer_price": 285000,
        "arv": 340000,
        "repair_estimate": 25000,
        "list_price": 320000,
        "days_on_market": 47,
        "agent_name": "...",
        "agent_phone": "...",
        "comps": [{"address":..., "sqft":..., "bed":..., "bath":...,
                   "sale_price":..., "sold_date":..., "price_sqft":...}, ...]
      }
    """
    body = await request.json()
    contact_id = body.pop("contact_id", "")
    if not contact_id:
        return JSONResponse({"error": "missing contact_id"}, status_code=400)

    from offer_comp_email import send_offer_comp_email
    success = send_offer_comp_email(contact_id, GHL_HEADERS, body)
    return {"success": success, "contact_id": contact_id}


@app.post("/analyze")
async def analyze_endpoint(request: Request):
    """Trigger offer analysis for a property.

    Body: { address, redfin_url, sqft, ghl_opportunity_id, persist }
    """
    body = await request.json()
    address = body.get("address")
    if not address:
        return JSONResponse({"error": "address required"}, status_code=400)

    result = _analyze_deal(
        address=address,
        redfin_url=body.get("redfin_url"),
        sqft=body.get("sqft", 1200),
        persist=body.get("persist", False),
        ghl_opportunity_id=body.get("ghl_opportunity_id"),
    )
    return result


# ── Buyer opt-in form ─────────────────────────────────────────────────────────

@app.get("/optin")
async def buyer_optin_form():
    """Serve the static buyer opt-in HTML form."""
    form_path = _STATIC_DIR / "buyer_optin.html"
    return FileResponse(form_path, media_type="text/html")


@app.post("/buyer-optin")
async def buyer_optin(request: Request):
    """
    Process buyer opt-in form submission. Writes directly to Supabase cash_buyers.

    Body (JSON):
      {
        "first_name":       "Alice",
        "last_name":        "Smith",
        "email":            "alice@example.com",
        "phone":            "+15550001111",
        "company":          "Smith RE LLC",
        "property_types":   ["multifamily", "retail"],
        "price_range_min":  500000,
        "price_range_max":  3000000,
        "preferred_states": ["CA", "TX"]
      }
    """
    body = await request.json()

    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, status_code=400)

    phone = (body.get("phone") or "").strip()
    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    property_types = "|".join(body.get("property_types") or [])

    record = {
        "first_name":      (body.get("first_name") or "").strip(),
        "last_name":       (body.get("last_name") or "").strip(),
        "email":           email,
        "phone":           phone,
        "company":         (body.get("company") or "").strip(),
        "price_range_min": float(body.get("price_range_min") or 0),
        "price_range_max": float(body.get("price_range_max") or 0),
        "preferred_states": body.get("preferred_states") or [],
        "buy_criteria":    {"property_type": property_types},
        "status":          "active",
    }

    from dispo_tracks import _get_sb
    try:
        sb = _get_sb()
        sb.table("cash_buyers").upsert(record, on_conflict="email").execute()
    except Exception as exc:
        log.error("/buyer-optin Supabase write failed email=%s: %s", email, exc)
        return JSONResponse({"error": "database error"}, status_code=500)

    log.info("Buyer opt-in: %s (%s)", email, phone)
    return {"success": True, "email": email}


# ── Universal AI SMS agent ─────────────────────────────────────────────────────

@app.post("/ghl-sms-ai")
async def ghl_sms_ai(request: Request):
    """
    Universal AI SMS reply handler — covers all contacts not handled by
    specialized tracks (dispo, MLS Track B).

    GHL workflow setup:
      Trigger: Customer Reply (SMS)
      Condition: Contact does NOT have tags mls-track-b-backup OR dispo-blast
                 (or add this as a parallel branch to the existing workflow)
      Action: Webhook POST https://<server>/ghl-sms-ai
      Body: {
        "contact_id":   "{{contact.id}}",
        "message":      "{{message.body}}",
        "from_number":  "{{message.phone}}"
      }

    Returns:
      { handled, intent, persona, sms_sent, call_triggered }
    """
    body = await request.json()
    contact_id  = (body.get("contact_id") or "").strip()
    message     = (body.get("message") or "").strip()
    from_number = (body.get("from_number") or "").strip()

    if not contact_id:
        return JSONResponse({"error": "missing contact_id"}, status_code=400)
    if not message:
        return JSONResponse({"error": "missing message"}, status_code=400)

    from sms_agent import handle_sms_ai
    result = handle_sms_ai(contact_id, message, from_number)
    log.info(
        "/ghl-sms-ai contact=%s intent=%s sms_sent=%s call=%s",
        contact_id,
        result.get("intent"),
        result.get("sms_sent"),
        result.get("call_triggered"),
    )
    return result
