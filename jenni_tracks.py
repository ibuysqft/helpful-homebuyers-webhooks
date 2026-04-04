"""
Jenni Track Automation — Commercial On Market (Crexi outbound)
Implements 6 post-call drip tracks triggered by Retell call_outcome.

Tracks:
  A: seller_finance_interest — immediate SF pitch email + Day3 follow-up SMS
  B: cash_offer_interest     — immediate cash MAO SMS + Day3 follow-up SMS
  C: both_interest           — immediate dual offer email + Day3 follow-up SMS
  D: financials_needed       — immediate data request SMS (re-trigger when received)
  E: no_answer / voicemail   — Day3 SMS + Day5 retry Jenni call
  F: already_in_escrow       — Day21 check-in SMS

Usage:
    from jenni_tracks import start_scheduler, stop_scheduler, dispatch_track

    # On FastAPI startup:
    start_scheduler()

    # After Jenni call ends (in /jenni-call-ended webhook):
    dispatch_track(
        contact_id, call_outcome, broker_name, address, to_number,
        asking_price=..., property_type=..., days_on_market=...,
        cap_rate=..., noi=..., unit_count=...,
    )

    # On FastAPI shutdown:
    stop_scheduler()
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

_GHL_KEY = os.getenv("GHL_API_KEY", "")
LOCATION_ID    = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
JENNI_AGENT_ID    = os.getenv("JENNI_AGENT_ID", "")
JENNI_PHONE       = os.getenv("JENNI_PHONE", "")         # GHL number — used for SMS fromNumber
JENNI_RETELL_PHONE = os.getenv("JENNI_RETELL_PHONE", "")  # Retell-Twilio — used for outbound calls

GHL_BASE    = "https://services.leadconnectorhq.com"
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
_jenni_jobstore_env = os.getenv("JENNI_TRACK_JOBSTORE", "")
JOBSTORE_URL = (
    _jenni_jobstore_env
    if _jenni_jobstore_env.startswith(("postgresql", "sqlite"))
    else "sqlite:////tmp/jenni_tracks.db"
)

# ── Scheduler ────────────────────────────────────────────────────────────────────

_scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=JOBSTORE_URL)},
    executors={"default": ThreadPoolExecutor(4)},
    timezone="America/Los_Angeles",
)


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
        log.info("Jenni track scheduler started (jobstore: %s)", JOBSTORE_PATH)


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Jenni track scheduler stopped")


# ── GHL helpers ──────────────────────────────────────────────────────────────────

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


def _send_sms(contact_id: str, message: str) -> bool:
    payload: dict = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
    }
    if JENNI_PHONE:
        payload["fromNumber"] = JENNI_PHONE
    r = _ghl_post("/conversations/messages", payload)
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("Jenni SMS sent → %s", contact_id)
    else:
        log.error("Jenni SMS FAILED → %s: %s", contact_id, r.status_code if r else "no response")
    return success


def _send_email(contact_id: str, subject: str, html_body: str) -> bool:
    payload: dict = {
        "type": "Email",
        "contactId": contact_id,
        "subject": subject,
        "html": html_body,
    }
    r = _ghl_post("/conversations/messages", payload)
    success = r is not None and r.status_code in (200, 201)
    log.info("Jenni email %s → %s", "sent" if success else "FAILED", contact_id)
    return success


def _add_tag(contact_id: str, tag: str) -> None:
    _ghl_post(f"/contacts/{contact_id}/tags", {"tags": [tag]})


def _add_note(contact_id: str, note: str) -> None:
    _ghl_post(f"/contacts/{contact_id}/notes", {"body": note, "userId": ""})


# ── Retell outbound call trigger ─────────────────────────────────────────────────

def trigger_jenni_call(
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
    context_note: str = "",
) -> bool:
    """
    Trigger an outbound Jenni call via Retell for a commercial Crexi lead.
    Requires JENNI_PHONE env var.
    """
    if not JENNI_RETELL_PHONE:
        log.warning("trigger_jenni_call: JENNI_RETELL_PHONE not set — cannot dial %s", contact_id)
        _add_note(
            contact_id,
            f"⚠️ Jenni call SKIPPED — JENNI_RETELL_PHONE not configured.\n"
            f"Broker: {broker_name} | {address}\nNote: {context_note}",
        )
        return False

    if not to_number:
        log.warning("trigger_jenni_call: no phone for contact %s", contact_id)
        return False

    payload = {
        "agent_id":   JENNI_AGENT_ID,
        "from_number": JENNI_RETELL_PHONE,
        "to_number":  to_number,
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
            log.info("Jenni call triggered → %s (%s) | %s", contact_id, to_number, address)
            _add_note(contact_id, f"📞 Jenni outbound call triggered for {address}. {context_note}")
        else:
            log.error("Jenni call FAILED %s: %s %s", contact_id, r.status_code, r.text[:200])
        return success
    except requests.RequestException as exc:
        log.error("trigger_jenni_call request failed: %s", exc)
        return False


# ── Template helpers ─────────────────────────────────────────────────────────────

def _tpl(template: str, name: str, address: str) -> str:
    first = name.split()[0] if name else "there"
    addr = address.strip() if address else "your listing"
    return template.format(name=first, address=addr)


# ── SMS Templates ────────────────────────────────────────────────────────────────

# Track A — seller finance interest
_TRACK_A_DAY3_SMS = (
    "Hey {name} — Jenni here! Just wanted to make sure the seller finance proposal "
    "for {address} landed okay. Happy to walk through the payment schedule live — "
    "takes 10 minutes and it usually makes everything click. When works for you?"
)

# Track B — cash offer interest
_TRACK_B_IMMEDIATE_SMS = (
    "Hey {name} — Jenni from Helpful Homebuyers! Great talking with you about {address}. "
    "I'm running the cash MAO now — I'll have a number to you within the hour. "
    "Just need the current NOI if you have it handy. Anything missing from what we discussed?"
)

_TRACK_B_DAY3_SMS = (
    "Hi {name} — Jenni here. Following up on the cash offer for {address}. "
    "Do you have the NOI and current occupancy? Once I have those I can get you "
    "a real number, not a guess. Takes two minutes — just reply here."
)

# Track C — both offer interest
_TRACK_C_DAY3_SMS = (
    "Hey {name} — Jenni! Just confirming you got both the seller finance terms "
    "and the cash offer for {address}. "
    "The seller finance option is genuinely the stronger one for most sellers — "
    "happy to walk through why on a 10-minute call. Interested?"
)

# Track D — financials needed
_TRACK_D_IMMEDIATE_SMS = (
    "Hey {name} — Jenni from Helpful Homebuyers! Great talking with you about {address}. "
    "To build the full offer package I just need:\n\n"
    "1. Current annual NOI (or gross rents + vacancy/expense breakdown)\n"
    "2. Current occupancy rate\n"
    "3. Is there an existing mortgage on it?\n\n"
    "Reply here and I'll have seller finance terms + cash offer back to you same day. — Jenni"
)

# Track E — no answer / voicemail
_TRACK_E_DAY3_SMS = (
    "Hey {name}, this is Jenni — I called about your listing at {address} on Crexi. "
    "Left you the most interesting voicemail you've gotten about this listing. "
    "Full asking price. Seriously. Call me back or just reply and I'll explain it. — Jenni"
)

# Track F — already in escrow
_TRACK_F_DAY21_SMS = (
    "Hey {name} — Jenni from Helpful Homebuyers checking in on {address}. "
    "Hope your current deal is moving smoothly! "
    "If anything changes on timing or terms, we're still very interested — "
    "full price, quick close. No pressure, just wanted to stay on your radar. — Jenni"
)


# ── Email builders ───────────────────────────────────────────────────────────────

def _sf_offer_email_html(broker_name: str, address: str, asking_price: str, noi: str) -> str:
    first = broker_name.split()[0] if broker_name else "there"
    price_display = asking_price if asking_price else "full asking price"
    noi_display = noi if noi and noi != "not listed" else "pending your numbers"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#1e293b;border-radius:12px;overflow:hidden;box-shadow:0 4px 32px rgba(0,0,0,.4);">
  <tr>
    <td style="background:linear-gradient(135deg,#1e3a5f 0%,#1e293b 100%);padding:32px 40px;border-bottom:1px solid #334155;">
      <p style="margin:0 0 6px;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;">Helpful Homebuyers — Commercial Division</p>
      <h1 style="margin:0;color:#f1f5f9;font-size:22px;font-weight:700;">Seller Finance Proposal</h1>
      <p style="margin:8px 0 0;color:#94a3b8;font-size:13px;">{address}</p>
    </td>
  </tr>
  <tr>
    <td style="padding:32px 40px;">
      <p style="margin:0 0 20px;color:#cbd5e1;font-size:15px;line-height:1.7;">Hi {first},</p>
      <p style="margin:0 0 20px;color:#cbd5e1;font-size:15px;line-height:1.7;">
        Great talking with you. Here's the seller finance structure I mentioned — the one where
        your seller gets their <strong style="color:#f1f5f9;">full {price_display}</strong>
        and becomes the bank instead of the customer.
      </p>

      <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;border-radius:10px;padding:24px;margin:0 0 24px;">
        <tr>
          <td>
            <p style="margin:0 0 12px;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;">Proposed Structure</p>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="color:#94a3b8;font-size:13px;padding:6px 0;border-bottom:1px solid #1e293b;">Purchase Price</td>
                <td align="right" style="color:#f1f5f9;font-size:13px;font-weight:600;padding:6px 0;border-bottom:1px solid #1e293b;">{price_display}</td>
              </tr>
              <tr>
                <td style="color:#94a3b8;font-size:13px;padding:6px 0;border-bottom:1px solid #1e293b;">Interest Rate</td>
                <td align="right" style="color:#10b981;font-size:13px;font-weight:600;padding:6px 0;border-bottom:1px solid #1e293b;">0%</td>
              </tr>
              <tr>
                <td style="color:#94a3b8;font-size:13px;padding:6px 0;border-bottom:1px solid #1e293b;">NOI (current)</td>
                <td align="right" style="color:#f1f5f9;font-size:13px;font-weight:600;padding:6px 0;border-bottom:1px solid #1e293b;">{noi_display}</td>
              </tr>
              <tr>
                <td style="color:#94a3b8;font-size:13px;padding:6px 0;">Structure</td>
                <td align="right" style="color:#f1f5f9;font-size:13px;font-weight:600;padding:6px 0;">Seller carry note — pure principal paydown</td>
              </tr>
            </table>
          </td>
        </tr>
      </table>

      <p style="margin:0 0 20px;color:#cbd5e1;font-size:15px;line-height:1.7;">
        Your seller goes from <em>paying</em> 7% to a bank to <em>collecting</em> 0% from us —
        every dollar of every payment is pure principal recovery. No income tax surprise
        on a lump sum. No reinvestment puzzle in this rate environment.
      </p>
      <p style="margin:0 0 28px;color:#cbd5e1;font-size:15px;line-height:1.7;">
        I'll build the full payment schedule once I have your current NOI and occupancy.
        Takes 15 minutes to put together — I'll have it same day.
      </p>

      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="background:#10b981;border-radius:8px;padding:12px 28px;">
            <a href="mailto:jenni@helpfulhomebuyersusa.com" style="color:#fff;font-size:14px;font-weight:700;text-decoration:none;">Reply to Jenni</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td style="background:#0f172a;border-top:1px solid #1e293b;padding:20px 40px;">
      <p style="margin:0;color:#475569;font-size:12px;line-height:1.6;">
        Jenni — Commercial Acquisitions, Helpful Homebuyers USA<br>
        <a href="https://helpfulhomebuyersusa.com" style="color:#10b981;">helpfulhomebuyersusa.com</a>
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""


def _dual_offer_email_html(broker_name: str, address: str, asking_price: str, noi: str) -> str:
    first = broker_name.split()[0] if broker_name else "there"
    price_display = asking_price if asking_price else "full asking price"
    noi_display = noi if noi and noi != "not listed" else "pending"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#1e293b;border-radius:12px;overflow:hidden;box-shadow:0 4px 32px rgba(0,0,0,.4);">
  <tr>
    <td style="background:linear-gradient(135deg,#1e3a5f 0%,#1e293b 100%);padding:32px 40px;border-bottom:1px solid #334155;">
      <p style="margin:0 0 6px;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;">Helpful Homebuyers — Commercial Division</p>
      <h1 style="margin:0;color:#f1f5f9;font-size:22px;font-weight:700;">Two Paths for Your Seller</h1>
      <p style="margin:8px 0 0;color:#94a3b8;font-size:13px;">{address}</p>
    </td>
  </tr>
  <tr>
    <td style="padding:32px 40px;">
      <p style="margin:0 0 20px;color:#cbd5e1;font-size:15px;line-height:1.7;">Hi {first},</p>
      <p style="margin:0 0 24px;color:#cbd5e1;font-size:15px;line-height:1.7;">
        Here are both options for your seller on <strong style="color:#f1f5f9;">{address}</strong>.
        Let them choose what fits their situation.
      </p>

      <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px;">
        <tr>
          <td width="48%" valign="top" style="background:#0f172a;border-radius:10px;padding:20px;border:1px solid #10b981;">
            <p style="margin:0 0 8px;color:#10b981;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;">Option 1 — Recommended</p>
            <p style="margin:0 0 4px;color:#f1f5f9;font-size:16px;font-weight:700;">Seller Finance</p>
            <p style="margin:0 0 12px;color:#64748b;font-size:12px;">Full price · Zero interest · Monthly income</p>
            <p style="margin:0;color:#94a3b8;font-size:13px;line-height:1.6;">
              Purchase price: <strong style="color:#f1f5f9;">{price_display}</strong><br>
              Interest: <strong style="color:#10b981;">0%</strong><br>
              NOI: {noi_display}<br>
              Your seller becomes the lender. Pure principal recovery every month.
            </p>
          </td>
          <td width="4%"></td>
          <td width="48%" valign="top" style="background:#0f172a;border-radius:10px;padding:20px;border:1px solid #334155;">
            <p style="margin:0 0 8px;color:#64748b;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;">Option 2</p>
            <p style="margin:0 0 4px;color:#f1f5f9;font-size:16px;font-weight:700;">Cash Close</p>
            <p style="margin:0 0 12px;color:#64748b;font-size:12px;">Below asking · Fast close · Clean exit</p>
            <p style="margin:0;color:#94a3b8;font-size:13px;line-height:1.6;">
              Purchase price: <strong style="color:#f1f5f9;">MAO (pending NOI)</strong><br>
              Close: <strong style="color:#f1f5f9;">14–21 days</strong><br>
              No contingencies · No repairs required<br>
              Best if seller needs liquidity now.
            </p>
          </td>
        </tr>
      </table>

      <p style="margin:0 0 28px;color:#cbd5e1;font-size:15px;line-height:1.7;">
        I'll have both fully built out for your 20-minute Deal Review.
        Once you send the NOI and occupancy I can finalize the numbers same day.
      </p>

      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="background:#10b981;border-radius:8px;padding:12px 28px;">
            <a href="mailto:jenni@helpfulhomebuyersusa.com" style="color:#fff;font-size:14px;font-weight:700;text-decoration:none;">Reply to Jenni</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td style="background:#0f172a;border-top:1px solid #1e293b;padding:20px 40px;">
      <p style="margin:0;color:#475569;font-size:12px;line-height:1.6;">
        Jenni — Commercial Acquisitions, Helpful Homebuyers USA<br>
        <a href="https://helpfulhomebuyersusa.com" style="color:#10b981;">helpfulhomebuyersusa.com</a>
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""


# ── Scheduled job functions (module-level for APScheduler pickling) ───────────────

def _job_send_sms(contact_id: str, message: str) -> None:
    _send_sms(contact_id, message)


def _job_send_email(contact_id: str, subject: str, html_body: str) -> None:
    _send_email(contact_id, subject, html_body)


def _job_jenni_retry_call(
    contact_id: str,
    broker_name: str,
    address: str,
    to_number: str,
    asking_price: str,
    property_type: str,
    days_on_market: str,
    cap_rate: str,
    noi: str,
    unit_count: str,
) -> None:
    trigger_jenni_call(
        contact_id, broker_name, address, to_number,
        asking_price=asking_price, property_type=property_type,
        days_on_market=days_on_market, cap_rate=cap_rate,
        noi=noi, unit_count=unit_count,
        context_note="Track E Day5 retry call — no answer on initial attempt.",
    )


# ── Schedule helper ───────────────────────────────────────────────────────────────

def _schedule(job_fn, run_at: datetime, job_id: str, *args) -> None:
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    _scheduler.add_job(
        job_fn,
        trigger="date",
        run_date=run_at,
        args=list(args),
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info("Scheduled %s at %s", job_id, run_at.isoformat())


# ── Track implementations ─────────────────────────────────────────────────────────

def track_a_seller_finance_interest(
    contact_id: str,
    broker_name: str,
    address: str,
    asking_price: str = "",
    noi: str = "not listed",
) -> None:
    """
    Track A: seller_finance_interest
    - Immediate: seller finance pitch email
    - Day 3: follow-up SMS
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_a_{contact_id}"

    html = _sf_offer_email_html(broker_name, address, asking_price, noi)
    subject = f"Seller Finance Proposal — {address}"

    _schedule(
        _job_send_email,
        now + timedelta(minutes=15),
        f"{prefix}_sf_email",
        contact_id,
        subject,
        html,
    )

    _schedule(
        _job_send_sms,
        now + timedelta(days=3),
        f"{prefix}_day3_sms",
        contact_id,
        _tpl(_TRACK_A_DAY3_SMS, broker_name, address),
    )

    _add_tag(contact_id, "jenni-track-a-sf-interest")
    _add_note(
        contact_id,
        f"💰 Track A started: SF email (15min) + Day3 follow-up SMS.\n"
        f"Asking: {asking_price} | NOI: {noi} | Address: {address}",
    )
    log.info("Track A started for contact %s", contact_id)


def track_b_cash_offer_interest(
    contact_id: str,
    broker_name: str,
    address: str,
    to_number: str,
    asking_price: str = "",
    noi: str = "not listed",
) -> None:
    """
    Track B: cash_offer_interest
    - Immediate SMS: MAO in progress, confirm NOI
    - Day 3: follow-up SMS
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_b_{contact_id}"

    _schedule(
        _job_send_sms,
        now + timedelta(minutes=5),
        f"{prefix}_immediate_sms",
        contact_id,
        _tpl(_TRACK_B_IMMEDIATE_SMS, broker_name, address),
    )

    _schedule(
        _job_send_sms,
        now + timedelta(days=3),
        f"{prefix}_day3_sms",
        contact_id,
        _tpl(_TRACK_B_DAY3_SMS, broker_name, address),
    )

    _add_tag(contact_id, "jenni-track-b-cash-interest")
    _add_note(
        contact_id,
        f"💵 Track B started: Immediate cash MAO SMS + Day3 follow-up.\n"
        f"Asking: {asking_price} | NOI: {noi} | Address: {address}",
    )
    log.info("Track B started for contact %s", contact_id)


def track_c_both_interest(
    contact_id: str,
    broker_name: str,
    address: str,
    asking_price: str = "",
    noi: str = "not listed",
) -> None:
    """
    Track C: both_interest
    - Immediate: dual offer email (SF + cash side-by-side)
    - Day 3: follow-up SMS
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_c_{contact_id}"

    html = _dual_offer_email_html(broker_name, address, asking_price, noi)
    subject = f"Two Paths for Your Seller — {address}"

    _schedule(
        _job_send_email,
        now + timedelta(minutes=15),
        f"{prefix}_dual_email",
        contact_id,
        subject,
        html,
    )

    _schedule(
        _job_send_sms,
        now + timedelta(days=3),
        f"{prefix}_day3_sms",
        contact_id,
        _tpl(_TRACK_C_DAY3_SMS, broker_name, address),
    )

    _add_tag(contact_id, "jenni-track-c-both-interest")
    _add_note(
        contact_id,
        f"🔀 Track C started: Dual offer email (15min) + Day3 SMS.\n"
        f"Asking: {asking_price} | NOI: {noi} | Address: {address}",
    )
    log.info("Track C started for contact %s", contact_id)


def track_d_financials_needed(
    contact_id: str,
    broker_name: str,
    address: str,
) -> None:
    """
    Track D: financials_needed
    - Immediate SMS: request NOI, occupancy, debt status
    (When financials arrive, caller creates a new track A/B/C)
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_d_{contact_id}"

    _schedule(
        _job_send_sms,
        now + timedelta(minutes=5),
        f"{prefix}_data_request_sms",
        contact_id,
        _tpl(_TRACK_D_IMMEDIATE_SMS, broker_name, address),
    )

    _add_tag(contact_id, "jenni-track-d-financials-needed")
    _add_note(
        contact_id,
        f"📊 Track D started: Immediate financial data request SMS.\n"
        f"Waiting on NOI + occupancy + debt status. Address: {address}",
    )
    log.info("Track D started for contact %s", contact_id)


def track_e_no_answer(
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
) -> None:
    """
    Track E: no_answer / voicemail
    - Day 3: curiosity SMS ("left you the most interesting voicemail")
    - Day 5: retry Jenni call
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_e_{contact_id}"

    _schedule(
        _job_send_sms,
        now + timedelta(days=3),
        f"{prefix}_day3_sms",
        contact_id,
        _tpl(_TRACK_E_DAY3_SMS, broker_name, address),
    )

    _schedule(
        _job_jenni_retry_call,
        now + timedelta(days=5),
        f"{prefix}_day5_call",
        contact_id,
        broker_name,
        address,
        to_number,
        asking_price,
        property_type,
        days_on_market,
        cap_rate,
        noi,
        unit_count,
    )

    _add_tag(contact_id, "jenni-track-e-no-answer")
    _add_note(
        contact_id,
        f"📬 Track E started: Day3 SMS + Day5 retry call.\nAddress: {address}",
    )
    log.info("Track E started for contact %s", contact_id)


def track_f_already_in_escrow(
    contact_id: str,
    broker_name: str,
    address: str,
) -> None:
    """
    Track F: already_in_escrow
    - Day 21: gentle check-in SMS (stay on radar for fallout)
    """
    now = datetime.now(tz=timezone.utc)
    prefix = f"jenni_f_{contact_id}"

    _schedule(
        _job_send_sms,
        now + timedelta(days=21),
        f"{prefix}_day21_sms",
        contact_id,
        _tpl(_TRACK_F_DAY21_SMS, broker_name, address),
    )

    _add_tag(contact_id, "jenni-track-f-in-escrow")
    _add_note(
        contact_id,
        f"⏳ Track F started: Day21 check-in SMS.\nAddress: {address}",
    )
    log.info("Track F started for contact %s", contact_id)


# ── Main dispatcher ───────────────────────────────────────────────────────────────

TRACK_MAP = {
    # Primary outcomes
    "seller_finance_interest": "A",
    "cash_offer_interest":     "B",
    "both_interest":           "C",
    "financials_needed":       "D",
    "no_answer":               "E",
    "voicemail":               "E",
    "already_in_escrow":       "F",
    # Normalised aliases Jenni might report
    "Seller Finance Interest":  "A",
    "Cash Offer Interest":      "B",
    "Both Interest":            "C",
    "Financials Needed":        "D",
    "No Answer":                "E",
    "Voicemail":                "E",
    "Already In Escrow":        "F",
    # No-action outcomes (tag + note only)
    "meeting_booked":           None,
    "not_interested":           None,
    "callback_requested":       None,
    "Meeting Booked":           None,
    "Not Interested":           None,
    "Callback Requested":       None,
}


def dispatch_track(
    contact_id: str,
    call_outcome: str,
    broker_name: str,
    address: str,
    to_number: str,
    asking_price: str = "",
    property_type: str = "",
    days_on_market: str = "",
    cap_rate: str = "not listed",
    noi: str = "not listed",
    unit_count: str = "N/A",
) -> Optional[str]:
    """
    Route a Jenni call outcome to the appropriate track.
    Returns the track letter dispatched, or None if no track matched / no-action outcome.

    Args:
        contact_id:      GHL contact ID
        call_outcome:    Retell call_analysis.custom_analysis_data.call_outcome
        broker_name:     Listing broker's full name
        address:         Property address
        to_number:       Broker phone number (for call tracks)
        asking_price:    Formatted list price, e.g. "$1,500,000"
        property_type:   e.g. "Multifamily"
        days_on_market:  e.g. "47"
        cap_rate:        e.g. "6.2%" or "not listed"
        noi:             e.g. "$88,000" or "not listed"
        unit_count:      e.g. "12" or "N/A"
    """
    if not _scheduler.running:
        log.warning("dispatch_track called but scheduler not running — starting it")
        start_scheduler()

    track = TRACK_MAP.get(call_outcome)

    if call_outcome not in TRACK_MAP:
        log.info("dispatch_track: unknown outcome '%s' for contact %s", call_outcome, contact_id)
        return None

    if track is None:
        # No-action outcome — tag and note only
        tag = f"jenni-{call_outcome.lower().replace(' ', '-').replace('_', '-')}"
        _add_tag(contact_id, tag)
        _add_note(
            contact_id,
            f"Jenni call outcome: {call_outcome}. No drip track triggered.\nAddress: {address}",
        )
        log.info("No-action outcome '%s' for contact %s — tagged only", call_outcome, contact_id)
        return None

    log.info(
        "Dispatching Track %s for contact %s | outcome=%s | address=%s",
        track, contact_id, call_outcome, address,
    )

    if track == "A":
        track_a_seller_finance_interest(contact_id, broker_name, address, asking_price, noi)
    elif track == "B":
        track_b_cash_offer_interest(contact_id, broker_name, address, to_number, asking_price, noi)
    elif track == "C":
        track_c_both_interest(contact_id, broker_name, address, asking_price, noi)
    elif track == "D":
        track_d_financials_needed(contact_id, broker_name, address)
    elif track == "E":
        track_e_no_answer(
            contact_id, broker_name, address, to_number,
            asking_price, property_type, days_on_market, cap_rate, noi, unit_count,
        )
    elif track == "F":
        track_f_already_in_escrow(contact_id, broker_name, address)

    return track
