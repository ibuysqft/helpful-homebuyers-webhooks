"""
MLS Track Automation — HHB On Market (Marcus agent)
Implements 4 post-call drip tracks triggered by Retell call_outcome.

Tracks:
  A: interested_write_offer  — 2hr offer+comp email → Day2 Marcus call
  B: in_escrow_backup        — Day7/21/30 SMS drip → any reply → instant Marcus call
  C: not_interested          — Day30/60/90 market data SMS → Day91 re-engage call
  D: voicemail               — 1hr SMS → 4hr email → Day2 call → Day4 SMS

Usage:
    from mls_tracks import start_scheduler, stop_scheduler, dispatch_track

    # On FastAPI startup:
    start_scheduler()

    # After Marcus call ends:
    dispatch_track(contact_id, call_outcome, name, address, phone)

    # On FastAPI shutdown:
    stop_scheduler()

Reply detection (Track B) is handled by the /ghl-inbound-sms webhook in main.py.
Call mls_tracks.handle_inbound_reply(contact_id) when a reply arrives.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────
_GHL_KEY = os.getenv("GHL_API_KEY_ON_MARKET") or os.getenv("GHL_API_KEY", "")
LOCATION_ID = "18Qc6ZWft7zdNY4oZUSm"
MARCUS_AGENT_ID = "agent_66939b0a2da6f2e37fe99edc54"
MARCUS_PHONE = os.getenv("MARCUS_PHONE", "")  # Set once phone provisioned in HHB On Market

# Commercial On Market (Grant)
COMMERCIAL_LOCATION_ID = "YJb8a3iGGQ1N2TQJM0yD"
GRANT_AGENT_ID = os.getenv("GRANT_AGENT_ID", "")
GRANT_PHONE    = os.getenv("GRANT_PHONE", "")

GHL_BASE = "https://services.leadconnectorhq.com"
RETELL_BASE = "https://api.retellai.com"

GHL_HEADERS = {
    "Authorization": f"Bearer {_GHL_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}

RETELL_HEADERS = {
    "Authorization": f"Bearer {os.getenv('RETELL_API_KEY', '')}",
    "Content-Type": "application/json",
}

# Supports both a plain file path (dev) and a full SQLAlchemy URL (prod Postgres).
_mls_jobstore_env = os.getenv("MLS_TRACK_JOBSTORE", "")
JOBSTORE_URL = (
    _mls_jobstore_env
    if _mls_jobstore_env.startswith(("postgresql", "sqlite"))
    else "sqlite:////tmp/mls_tracks.db"
)

# ── Scheduler ───────────────────────────────────────────────────────────────────
_scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=JOBSTORE_URL)},
    executors={"default": ThreadPoolExecutor(4)},
    timezone="America/Los_Angeles",
)


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
        log.info("MLS track scheduler started (jobstore: %s)", JOBSTORE_PATH)


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("MLS track scheduler stopped")


# ── GHL helpers ─────────────────────────────────────────────────────────────────

def _ghl_post(path: str, payload: dict) -> Optional[requests.Response]:
    try:
        r = requests.post(
            f"{GHL_BASE}{path}",
            headers=GHL_HEADERS,
            json=payload,
            timeout=15,
        )
        return r
    except requests.RequestException as exc:
        log.error("GHL POST %s failed: %s", path, exc)
        return None


def _get_contact(contact_id: str) -> dict:
    """Return contact dict from GHL, or empty dict on failure."""
    try:
        r = requests.get(
            f"{GHL_BASE}/contacts/{contact_id}",
            headers=GHL_HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("contact", r.json())
    except requests.RequestException as exc:
        log.error("_get_contact %s: %s", contact_id, exc)
    return {}


def _send_sms(contact_id: str, message: str, from_number: str = "") -> bool:
    payload: dict = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
    }
    if from_number:
        payload["fromNumber"] = from_number
    r = _ghl_post("/conversations/messages", payload)
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("MLS SMS sent → %s", contact_id)
    else:
        log.error("MLS SMS FAILED → %s: %s", contact_id, r.status_code if r else "no response")
    return success


def _send_email(contact_id: str, subject: str, html_body: str, to_email: str = "") -> bool:
    payload: dict = {
        "type": "Email",
        "contactId": contact_id,
        "subject": subject,
        "html": html_body,
    }
    if to_email:
        payload["emailTo"] = to_email
    r = _ghl_post("/conversations/messages", payload)
    success = r is not None and r.status_code in (200, 201)
    log.info("MLS email %s → %s", "sent" if success else "FAILED", contact_id)
    return success


def _add_tag(contact_id: str, tag: str) -> None:
    _ghl_post(f"/contacts/{contact_id}/tags", {"tags": [tag]})


def _add_note(contact_id: str, note: str) -> None:
    _ghl_post(f"/contacts/{contact_id}/notes", {"body": note, "userId": ""})


# ── Retell outbound call trigger ─────────────────────────────────────────────────

def trigger_marcus_call(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
    context_note: str = "",
) -> bool:
    """
    Trigger an outbound Marcus call via Retell API.
    Requires MARCUS_PHONE env var to be set (On Market phone number).
    """
    if not MARCUS_PHONE:
        log.warning("trigger_marcus_call: MARCUS_PHONE not set — cannot dial %s", contact_id)
        _add_note(
            contact_id,
            f"⚠️ Marcus call SKIPPED — MARCUS_PHONE env var not configured.\n"
            f"Contact: {contact_name} | {address}\n"
            f"Note: {context_note}",
        )
        return False

    if not to_number:
        log.warning("trigger_marcus_call: no phone for contact %s", contact_id)
        return False

    payload = {
        "agent_id": MARCUS_AGENT_ID,
        "from_number": MARCUS_PHONE,
        "to_number": to_number,
        "retell_llm_dynamic_variables": {
            "contact_id": contact_id,
            "contact_name": contact_name,
            "property_address": address,
            "lead_type": "mls_on_market",
            "call_context": context_note,
        },
    }

    try:
        r = requests.post(
            f"{RETELL_BASE}/v2/create-phone-call",
            headers=RETELL_HEADERS,
            json=payload,
            timeout=15,
        )
        success = r.status_code in (200, 201)
        if success:
            log.info("Marcus call triggered → %s (%s)", contact_id, to_number)
            _add_note(contact_id, f"📞 Marcus outbound call triggered. {context_note}")
        else:
            log.error("Marcus call FAILED %s: %s %s", contact_id, r.status_code, r.text[:200])
        return success
    except requests.RequestException as exc:
        log.error("trigger_marcus_call request failed: %s", exc)
        return False


# ── SMS Templates ────────────────────────────────────────────────────────────────

def _tpl(template: str, name: str, address: str) -> str:
    first = name.split()[0] if name else "there"
    addr = address.strip() if address else "your property"
    return template.format(name=first, address=addr)


_TRACK_A_DAY2_NOTE = (
    "Hey {name} — Blair from Helpful Home Buyers. "
    "Just following up on the offer we sent for {address}. "
    "Do you have any questions about the numbers? We're flexible and move fast. "
    "Let me know when's a good time to connect."
)

_TRACK_B_SMS = {
    7: (
        "Hey {name}, Blair from Helpful Home Buyers. "
        "Quick check-in on {address} — if the current deal falls through or you want a backup offer, "
        "we can move within 48 hours. Still interested? Just reply YES."
    ),
    21: (
        "Hi {name}, Blair here. Still thinking about {address}? "
        "Market's been moving — I can get you updated comps and a refreshed offer this week. "
        "Want me to pull them? Reply YES."
    ),
    30: (
        "Hey {name} — last check-in from Blair at Helpful Home Buyers. "
        "We're still ready to close on {address} fast if your current deal changes. "
        "No pressure — just here when you need us. Reply anytime."
    ),
}

_TRACK_C_SMS = {
    30: (
        "Hi {name}, Blair from Helpful Home Buyers. "
        "Quick market update on your area — homes near {address} have been selling "
        "faster and at higher prices this month. Conditions may have changed since we last spoke. "
        "Want a current market analysis? Reply YES."
    ),
    60: (
        "Hey {name} — Blair from Helpful Home Buyers checking in. "
        "We helped another family near {address} close in 9 days last month — they were in a similar situation. "
        "If your plans have changed, we can move quickly. No obligation to chat. Reply and I'll reach out."
    ),
    90: (
        "Hi {name}, this is Blair from Helpful Home Buyers. "
        "It's been 90 days since we connected about {address}. "
        "We're still buying in your area — same terms, fast close, no repairs needed. "
        "Is now a better time to talk? Just reply YES and I'll give you a call."
    ),
}

_TRACK_D_1HR_SMS = (
    "Hey {name}, Blair from Helpful Home Buyers. "
    "Left you a voicemail about {address}. We buy homes as-is for cash — fast close, no fees. "
    "When's a good time to connect? Just reply here."
)

_TRACK_D_DAY4_SMS = (
    "Hi {name} — Blair from Helpful Home Buyers. "
    "One last check-in about {address}. If you're not interested, no worries at all — just say the word. "
    "If timing changes or you want a cash offer, I'm one reply away. "
    "Hope things are going well!"
)


def _track_d_4hr_email_html(name: str, address: str) -> str:
    first = name.split()[0] if name else "there"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
  <tr>
    <td style="background:#0f172a;padding:28px 40px;">
      <p style="margin:0 0 4px;color:#94a3b8;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;">Helpful Home Buyers USA</p>
      <h1 style="margin:0;color:#f8fafc;font-size:22px;font-weight:700;">Still Trying to Reach You</h1>
    </td>
  </tr>
  <tr>
    <td style="padding:32px 40px;">
      <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.7;">Hi {first},</p>
      <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.7;">
        I tried calling and texting about <strong>{address}</strong>. I don't want to keep bothering you —
        but I did want to make sure you got the message before I close out the file.
      </p>
      <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.7;">
        We buy homes <strong>as-is for cash</strong> — no repairs, no agent commissions, no open houses.
        We can close in as little as <strong>7–14 days</strong> or on your timeline.
      </p>
      <p style="margin:0 0 24px;color:#374151;font-size:15px;line-height:1.7;">
        If this isn't a good fit, no hard feelings — just let me know and I'll respect that.
        If you are curious, just hit reply and I'll get you a real number within 24 hours.
      </p>
      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="background:#10b981;border-radius:8px;padding:12px 24px;">
            <a href="tel:+17039401159" style="color:#fff;font-size:14px;font-weight:700;text-decoration:none;">Call Blair: (703) 940-1159</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:20px 40px;">
      <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
        Blair — Helpful Home Buyers USA<br>
        <a href="https://helpfulhomebuyersusa.com" style="color:#10b981;">helpfulhomebuyersusa.com</a>
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""


# ── Scheduled job functions (must be module-level for APScheduler pickling) ──────

def _job_send_sms(contact_id: str, message: str) -> None:
    _send_sms(contact_id, message)


def _job_send_email(contact_id: str, subject: str, html_body: str) -> None:
    _send_email(contact_id, subject, html_body)


def _job_trigger_call(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
    context_note: str,
) -> None:
    trigger_marcus_call(contact_id, contact_name, address, to_number, context_note)


def _job_track_c_call(contact_id: str, contact_name: str, address: str, to_number: str) -> None:
    trigger_marcus_call(
        contact_id, contact_name, address, to_number,
        context_note="Track C Day91 re-engagement call — was not interested 91 days ago.",
    )


# ── Track implementations ────────────────────────────────────────────────────────

def _schedule(job_fn, run_at: datetime, job_id: str, *args) -> None:
    """Schedule a job; replace if already exists."""
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    _scheduler.add_job(
        job_fn,
        trigger="date",
        run_date=run_at,
        args=list(args),
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,  # 1hr grace for missed jobs
    )
    log.info("Scheduled %s at %s", job_id, run_at.isoformat())


def track_a_interested_write_offer(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
    offer_data: Optional[dict] = None,
) -> None:
    """
    Track A: interested_write_offer
    - 2hr: Offer + Comp email
    - Day 2: Marcus follow-up call
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"track_a_{contact_id}"

    # 2-hour offer + comp email
    if offer_data is None:
        offer_data = {}
    offer_data.setdefault("contact_name", contact_name)
    offer_data.setdefault("address", address)

    # Import here to avoid circular issues at module load time
    from offer_comp_email import build_offer_comp_html
    html = build_offer_comp_html(offer_data)
    subject = f"Your Offer Package — {address}"

    _schedule(
        _job_send_email,
        now + timedelta(hours=2),
        f"{prefix}_2hr_email",
        contact_id,
        subject,
        html,
    )

    # Day 2 call
    _schedule(
        _job_trigger_call,
        now + timedelta(days=2),
        f"{prefix}_day2_call",
        contact_id,
        contact_name,
        address,
        to_number,
        "Track A follow-up — seller expressed interest, offer submitted.",
    )

    _add_tag(contact_id, "mls-track-a-offer-sent")
    _add_note(
        contact_id,
        f"🏠 Track A started: Offer + Comp email scheduled (2hr) + Day2 call.\nAddress: {address}",
    )
    log.info("Track A started for contact %s", contact_id)


def track_b_in_escrow_backup(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
) -> None:
    """
    Track B: in_escrow_backup
    - Day 7/21/30 SMS drip
    - Any inbound reply → instant Marcus call (handled by /ghl-inbound-sms webhook)
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"track_b_{contact_id}"

    for day, template in _TRACK_B_SMS.items():
        msg = _tpl(template, contact_name, address)
        _schedule(
            _job_send_sms,
            now + timedelta(days=day),
            f"{prefix}_day{day}_sms",
            contact_id,
            msg,
        )

    _add_tag(contact_id, "mls-track-b-backup")
    _add_note(
        contact_id,
        f"⏳ Track B started: Day7/21/30 SMS drip. Reply will trigger instant Marcus call.\nAddress: {address}",
    )
    log.info("Track B started for contact %s", contact_id)


def track_c_not_interested(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
) -> None:
    """
    Track C: not_interested
    - Day 30/60/90 market re-engagement SMS
    - Day 91: Marcus re-engagement call
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"track_c_{contact_id}"

    for day, template in _TRACK_C_SMS.items():
        msg = _tpl(template, contact_name, address)
        _schedule(
            _job_send_sms,
            now + timedelta(days=day),
            f"{prefix}_day{day}_sms",
            contact_id,
            msg,
        )

    # Day 91: re-engagement call
    _schedule(
        _job_track_c_call,
        now + timedelta(days=91),
        f"{prefix}_day91_call",
        contact_id,
        contact_name,
        address,
        to_number,
    )

    _add_tag(contact_id, "mls-track-c-not-interested")
    _add_note(
        contact_id,
        f"❄️ Track C started: Day30/60/90 market re-engagement SMS + Day91 call.\nAddress: {address}",
    )
    log.info("Track C started for contact %s", contact_id)


def track_d_voicemail(
    contact_id: str,
    contact_name: str,
    address: str,
    to_number: str,
) -> None:
    """
    Track D: voicemail
    - 1hr: SMS
    - 4hr: Email
    - Day 2: Call
    - Day 4: Final SMS
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"track_d_{contact_id}"

    # 1hr SMS
    _schedule(
        _job_send_sms,
        now + timedelta(hours=1),
        f"{prefix}_1hr_sms",
        contact_id,
        _tpl(_TRACK_D_1HR_SMS, contact_name, address),
    )

    # 4hr email
    html = _track_d_4hr_email_html(contact_name, address)
    _schedule(
        _job_send_email,
        now + timedelta(hours=4),
        f"{prefix}_4hr_email",
        contact_id,
        f"Still trying to reach you — {address}",
        html,
    )

    # Day 2 call
    _schedule(
        _job_trigger_call,
        now + timedelta(days=2),
        f"{prefix}_day2_call",
        contact_id,
        contact_name,
        address,
        to_number,
        "Track D Day2 call — left voicemail, no response to SMS/email yet.",
    )

    # Day 4 final SMS
    _schedule(
        _job_send_sms,
        now + timedelta(days=4),
        f"{prefix}_day4_sms",
        contact_id,
        _tpl(_TRACK_D_DAY4_SMS, contact_name, address),
    )

    _add_tag(contact_id, "mls-track-d-voicemail")
    _add_note(
        contact_id,
        f"📬 Track D started: 1hr SMS → 4hr email → Day2 call → Day4 SMS.\nAddress: {address}",
    )
    log.info("Track D started for contact %s", contact_id)


# ── Track B reply handler ────────────────────────────────────────────────────────

def handle_inbound_reply(contact_id: str, message_body: str = "") -> bool:
    """
    Called when an inbound SMS arrives for a contact tagged mls-track-b-backup.
    Cancels pending Track B jobs and triggers an instant Marcus call.
    Returns True if a call was triggered.
    """
    # Fetch contact for name/phone/address
    contact = _get_contact(contact_id)
    if not contact:
        log.warning("handle_inbound_reply: contact %s not found", contact_id)
        return False

    name = (contact.get("firstName") or "") + " " + (contact.get("lastName") or "")
    name = name.strip() or "there"
    to_number = contact.get("phone", "")
    address = contact.get("customField", {}).get("mls_address", "") or ""

    # Cancel pending Track B jobs
    prefix = f"track_b_{contact_id}"
    for day in (7, 21, 30):
        job_id = f"{prefix}_day{day}_sms"
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
            log.info("Cancelled %s (reply received)", job_id)

    if not to_number:
        log.warning("handle_inbound_reply: no phone on contact %s", contact_id)
        _add_note(
            contact_id,
            f"📱 Track B reply received: '{message_body[:100]}'\n"
            f"⚠️ Cannot trigger Marcus call — no phone number on contact.",
        )
        return False

    _add_note(
        contact_id,
        f"📱 Track B reply received: '{message_body[:200]}'\n"
        f"⚡ Triggering instant Marcus call...",
    )

    return trigger_marcus_call(
        contact_id,
        name,
        address,
        to_number,
        context_note=f"Track B instant reply trigger. Message: '{message_body[:100]}'",
    )


# ── Main dispatcher ──────────────────────────────────────────────────────────────

# Retell call_outcome values Marcus uses for MLS calls
TRACK_MAP = {
    "interested_write_offer": "A",
    "in_escrow_backup": "B",
    "not_interested": "C",
    "voicemail": "D",
    # Aliases / variations Marcus might report
    "Voicemail": "D",
    "Not Interested": "C",
    "In Escrow - Backup": "B",
    "Interested - Write Offer": "A",
}


def dispatch_track(
    contact_id: str,
    call_outcome: str,
    contact_name: str,
    address: str,
    to_number: str,
    offer_data: Optional[dict] = None,
) -> Optional[str]:
    """
    Route a Marcus call outcome to the appropriate track.
    Returns the track letter dispatched, or None if no track matched.

    Args:
        contact_id: GHL contact ID
        call_outcome: Retell call_analysis.custom_analysis_data.call_outcome
        contact_name: Full name for personalization
        address: Property address
        to_number: Contact phone number
        offer_data: Optional dict for Track A email (offer_price, arv, comps, etc.)
    """
    if not _scheduler.running:
        log.warning("dispatch_track called but scheduler not running — starting it")
        start_scheduler()

    track = TRACK_MAP.get(call_outcome)
    if track is None:
        log.info("dispatch_track: no MLS track for outcome '%s'", call_outcome)
        return None

    log.info(
        "Dispatching Track %s for contact %s | outcome=%s | address=%s",
        track, contact_id, call_outcome, address,
    )

    if track == "A":
        track_a_interested_write_offer(contact_id, contact_name, address, to_number, offer_data)
    elif track == "B":
        track_b_in_escrow_backup(contact_id, contact_name, address, to_number)
    elif track == "C":
        track_c_not_interested(contact_id, contact_name, address, to_number)
    elif track == "D":
        track_d_voicemail(contact_id, contact_name, address, to_number)

    return track


# ── Grant — Commercial On Market ──────────────────────────────────────────────

def trigger_grant_call(
    contact_id: str,
    broker_name: str,
    address: str,
    to_number: str,
    asking_price: str = "",
    property_type: str = "",
    days_on_market: str = "",
    cap_rate: str = "not listed",
    noi: str = "not listed",
    unit_count: str = "N/A",
) -> bool:
    """
    Trigger an outbound Grant call via Retell for a commercial Crexi lead.
    Requires GRANT_PHONE and GRANT_AGENT_ID env vars.
    """
    if not GRANT_PHONE or not GRANT_AGENT_ID:
        log.warning("trigger_grant_call: GRANT_PHONE or GRANT_AGENT_ID not set — skipping %s", contact_id)
        _add_note(
            contact_id,
            f"⚠️ Grant call SKIPPED — GRANT_PHONE or GRANT_AGENT_ID not configured.\n"
            f"Broker: {broker_name} | {address}",
        )
        return False

    if not to_number:
        log.warning("trigger_grant_call: no phone for contact %s", contact_id)
        return False

    payload = {
        "agent_id": GRANT_AGENT_ID,
        "from_number": GRANT_PHONE,
        "to_number": to_number,
        "retell_llm_dynamic_variables": {
            "contact_id":       contact_id,
            "name":             broker_name,
            "property_address": address,
            "asking_price":     asking_price,
            "property_type":    property_type,
            "days_on_market":   days_on_market,
            "cap_rate":         cap_rate,
            "noi":              noi,
            "unit_count":       unit_count,
        },
    }

    try:
        r = requests.post(
            f"{RETELL_BASE}/v2/create-phone-call",
            headers=RETELL_HEADERS,
            json=payload,
            timeout=15,
        )
        success = r.status_code in (200, 201)
        if success:
            log.info("Grant call triggered → %s (%s) | %s", contact_id, to_number, address)
            _add_note(contact_id, f"📞 Grant outbound call triggered for {address}.")
        else:
            log.error("Grant call FAILED %s: %s %s", contact_id, r.status_code, r.text[:200])
        return success
    except requests.RequestException as exc:
        log.error("trigger_grant_call request failed: %s", exc)
        return False
