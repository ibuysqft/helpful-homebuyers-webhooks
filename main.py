"""
Helpful Homebuyers — Webhook Server v3
Handles all 4 Retell agents + full AI follow-up sequence system.

Routes:
  POST /{agent}-check-calendar       (agent = shelby|alex|cole|jordan)
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
from fastapi.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────
GHL_API_KEY     = os.getenv("GHL_API_KEY", "pit-db848c79-dc09-4ba7-aadf-7a21db5f30d1")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
CALENDAR_ID     = os.getenv("CALENDAR_ID", "BqJ0rjqAFgh7VMJUvI5U")
GHL_BASE        = "https://services.leadconnectorhq.com"

# Appointment duration in minutes (real estate consultations = 30 min min)
APPT_DURATION_MIN = int(os.getenv("APPT_DURATION_MIN", "30"))

GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}

# Map Retell call outcomes → GHL pipeline stage names
STAGE_MAP = {
    "Appointment Set":        "AI - Qualified (Appt Set)",
    "Attorney Intro Agreed":  "AI - Qualified (Appt Set)",
    "Seeds Planted":          "AI - Qualified (Seeds Planted)",
    "Micro-Commitment":       "AI - Qualified (Seeds Planted)",
    "Call Back Later":        "AI - Qualified (Seeds Planted)",
    "Interested - Reviewing": "AI - Qualified (Seeds Planted)",
    "Not Ready":              "AI - Qualified (Seeds Planted)",
    "Voicemail":              "AI - No Answer",
    "No Answer":              "AI - No Answer",
    "Not Interested":         "Dead - Not Interested",
    "Disqualified":           "Dead - DQ",
    "DQ - Not Heir":          "Dead - DQ",
    "DQ - Already Sold":      "Dead - DQ",
    "DQ - Active Litigation": "Dead - DQ",
    "Wrong Number":           "Dead - DQ",
    "Disconnected":           "Dead - DQ",
}

# Flags/urgency values that trigger escalation
URGENT_FLAGS = {"urgent_under_14_days", "critical_-_under_14_days"}

VALID_AGENTS = {"shelby", "alex", "cole", "jordan"}

# ── Retell agent_id → GHL outbound phone number ───────────────────────────────
AGENT_PHONE_MAP = {
    "agent_bde1f8ca91b3a63a42ecad9777": "+17036915670",  # Shelby inbound
    "agent_40da2f733e42df807a89c669d6": "+17036915670",  # Shelby outbound
    "agent_636dd8ac10f4b633ab38bb001e": "+17038402238",  # Alex bankruptcy
    "agent_e6cafef912272207148d11893f": "+17038402238",  # Alex bankruptcy alt
    "agent_56e1def11bd5201bcdc1fedd6b": "+12133720548",  # Cole acquisitions
    "agent_dd0928ae5479516c905c55ca4d": "+12134747691",  # Jordan estate
}

# ── Outcome-based SMS templates (NEPQ/Hormozi style) ─────────────────────────
# {name} = contact first name, {address} = property address
SMS_TEMPLATES: dict[str, Optional[str]] = {
    "Appointment Set": (
        "Hey {name}! Shelby from Helpful Homebuyers. You're all set ✅ "
        "We'll go over your options for {address} and put together a real cash number for you. "
        "If anything changes, just reply here. Talk soon!"
    ),
    "Attorney Intro Agreed": (
        "Hey {name}, Shelby from Helpful Homebuyers. "
        "Really appreciate you chatting today. We'll get our attorney connected with yours — "
        "we handle everything on our end. Any questions, just reply here."
    ),
    "Seeds Planted": (
        "Hey {name}, Shelby from Helpful Homebuyers. "
        "Quick question — what would getting a fair cash offer on {address} this week actually mean for your situation? "
        "No repairs, no fees, we close on your timeline. Just reply and let me know."
    ),
    "Micro-Commitment": (
        "Hey {name} — Shelby here. "
        "Really glad we connected. We can move fast on {address} when you're ready. "
        "What's one thing that would need to happen for this to make sense for you? Just reply."
    ),
    "Interested - Reviewing": (
        "Hey {name}, this is Shelby from Helpful Homebuyers. "
        "Take your time — we're not going anywhere. "
        "When you're ready to talk numbers on {address}, just reply and I'll get a cash offer to you same day."
    ),
    "Call Back Later": (
        "Hey {name}, Shelby from Helpful Homebuyers. No worries at all — "
        "whenever the timing feels right, just reply here and we'll get moving. "
        "We close fast and handle everything."
    ),
    "Not Ready": (
        "Hey {name}, this is Shelby from Helpful Homebuyers. "
        "Totally understand — no rush. When things change with {address}, "
        "just reply and we'll be ready. We can close in as little as 7 days when you are."
    ),
    "Voicemail": (
        "Hey {name}, this is Shelby from Helpful Homebuyers. "
        "Left you a voicemail about {address}. We buy houses as-is for cash — no repairs, no fees. "
        "When's a good time to connect? Just reply here."
    ),
    "No Answer": (
        "Hey {name}, Shelby from Helpful Homebuyers here. "
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

app = FastAPI(title="Helpful Homebuyers Webhooks", version="2.0.0")

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
                wait = 2 ** attempt
                log.warning("GHL rate limit, retry in %ds", wait)
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

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    log.info("Helpful Homebuyers Webhook Server v2 starting")
    _load_pipeline_cache()

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "pipeline_stages_cached": sum(len(v) for v in _pipeline_cache.values()),
        "appt_duration_min": APPT_DURATION_MIN,
    }

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

    start_ms, end_ms = _day_timestamps(date)
    r = _ghl_get(
        f"/calendars/{CALENDAR_ID}/free-slots",
        params={"startDate": start_ms, "endDate": end_ms, "timezone": tz},
    )

    if not r or r.status_code != 200:
        log.error("[%s] check-calendar failed: %s", agent, r.status_code if r else "no response")
        return JSONResponse({"error": "calendar unavailable"}, status_code=502)

    slots = _format_slots(r.json(), tz)
    log.info("[%s] check-calendar %s → %d slots", agent, date, len(slots))
    return {"available_slots": slots, "count": len(slots), "date": date}

# ── Book appointment ──────────────────────────────────────────────────────────

@app.post("/{agent}-book-appointment")
async def book_appointment(agent: str, request: Request):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail="Unknown agent")

    body       = await request.json()
    contact_id = body.get("contact_id")
    start_time = body.get("start_time")
    title      = body.get("title", "Helpful Homebuyers Consultation")
    notes      = body.get("notes", "")

    if not contact_id or not start_time:
        return JSONResponse({"error": "Missing: contact_id, start_time"}, status_code=400)

    # Verify contact exists before booking
    if not _verify_contact(contact_id):
        log.error("[%s] Contact not found: %s", agent, contact_id)
        return JSONResponse({"error": f"Contact {contact_id} not found in GHL"}, status_code=404)

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse({"error": f"Invalid start_time: {start_time}"}, status_code=400)

    end_dt = start_dt + timedelta(minutes=APPT_DURATION_MIN)

    payload = {
        "calendarId":        CALENDAR_ID,
        "locationId":        GHL_LOCATION_ID,
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
    log.info("[%s] booked %s for contact %s (%dmin)", agent, appt_id, contact_id, APPT_DURATION_MIN)

    return {
        "success":            True,
        "appointment_id":     appt_id,
        "start_time_display": start_dt.strftime("%A, %B %-d at %-I:%M %p"),
        "duration_minutes":   APPT_DURATION_MIN,
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
    sms_sent = _send_followup_sms(contact_id, call_outcome, contact_name, prop_address, from_number)

    return {
        "success":       True,
        "contact_id":    contact_id,
        "outcome":       call_outcome,
        "stage":         ghl_stage,
        "stage_updated": stage_updated,
        "note_added":    note_id is not None,
        "email_updated": bool(email_captured),
        "urgent":        is_urgent,
        "sms_sent":      sms_sent,
        "tags_applied":  tags_applied,
    }
