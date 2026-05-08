"""
Universal AI SMS Agent — Helpful Homebuyers USA
Handles all inbound SMS not covered by specialized tracks (dispo, MLS Track B).

Flow:
  1. Fetch contact details + conversation history from GHL
  2. Select agent persona by inbound phone number
  3. Classify intent with Claude Haiku (fast + cheap)
  4. Generate NEPQ reply with Claude Sonnet
  5. Send SMS via GHL
  6. Apply intent tags to contact
  7. Trigger Retell outbound call on "hot" intent
"""

import logging
import os
from typing import Optional

import anthropic
import requests

from sms_personas import get_persona, persona_from_phone, PERSONA_RETELL_AGENT, PERSONA_PHONE

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
GHL_BASE        = "https://services.leadconnectorhq.com"
GHL_API_KEY     = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
RETELL_BASE     = "https://api.retellai.com"
RETELL_API_KEY  = os.getenv("RETELL_API_KEY", "")

GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version":       "2021-07-28",
    "Content-Type":  "application/json",
}
RETELL_HEADERS = {
    "Authorization": f"Bearer {RETELL_API_KEY}",
    "Content-Type":  "application/json",
}

ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

HISTORY_LIMIT = 10  # messages to pull for context

# Intent → GHL tag
INTENT_TAGS: dict[str, list[str]] = {
    "hot":         ["ai-sms-hot", "ai-warm-lead"],
    "appointment": ["ai-sms-appointment", "ai-warm-lead"],
    "question":    ["ai-sms-question", "ai-warm-lead"],
    "objection":   ["ai-sms-objection", "ai-warm-lead"],
    "warm":        ["ai-sms-warm"],
    "cold":        ["ai-sms-cold"],
    "stop":        ["ai-sms-stop", "ai-opted-out"],
    "dead":        ["ai-sms-dead"],
}

STOP_KEYWORDS = {"stop", "unsubscribe", "remove", "optout", "opt out", "opt-out", "cancel"}


# ── GHL helpers (local, avoids circular import from main.py) ──────────────────

def _ghl(method: str, path: str, **kwargs) -> Optional[requests.Response]:
    kwargs.setdefault("headers", GHL_HEADERS)
    kwargs.setdefault("timeout", 15)
    url = f"{GHL_BASE}{path}"
    try:
        r = requests.request(method, url, **kwargs)
        r.raise_for_status()
        return r
    except requests.HTTPError as exc:
        log.error("GHL %s %s → %s %s", method, path, exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:
        log.error("GHL %s %s failed: %s", method, path, exc)
        return None


def _ghl_get(path: str, **kw) -> Optional[requests.Response]:
    return _ghl("GET", path, **kw)


def _ghl_post(path: str, **kw) -> Optional[requests.Response]:
    return _ghl("POST", path, **kw)


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_contact(contact_id: str) -> dict:
    r = _ghl_get(f"/contacts/{contact_id}")
    if r:
        data = r.json()
        return data.get("contact", data) or {}
    return {}


def fetch_conversation_history(contact_id: str) -> list[dict]:
    """
    Returns last HISTORY_LIMIT messages as [{role, text}] oldest-first.
    GHL conversation search → get messages from conversation ID.
    """
    search_r = _ghl_get(
        "/conversations/search",
        params={"locationId": GHL_LOCATION_ID, "contactId": contact_id, "limit": 1},
    )
    if not search_r:
        return []

    conversations = search_r.json().get("conversations", [])
    if not conversations:
        return []

    conv_id = conversations[0].get("id", "")
    if not conv_id:
        return []

    msg_r = _ghl_get(
        f"/conversations/{conv_id}/messages",
        params={"limit": HISTORY_LIMIT},
    )
    if not msg_r:
        return []

    raw_messages = msg_r.json().get("messages", {}).get("messages", [])

    history: list[dict] = []
    for m in reversed(raw_messages):  # oldest first
        direction = m.get("direction", "")
        body = (m.get("body") or m.get("message") or "").strip()
        if not body:
            continue
        role = "contact" if direction == "inbound" else "agent"
        history.append({"role": role, "text": body})

    return history


# ── Intent classification ──────────────────────────────────────────────────────

_INTENT_SYSTEM = """Classify the intent of this inbound SMS from a real estate seller lead.
Reply with EXACTLY one word from this list:
  hot        - actively wants to sell, asks price/timeline, says "yes let's do it"
  appointment - wants to schedule a call or meeting
  question   - asking about process, repairs, timeline, fees, how it works
  objection  - hesitant, needs to think, "maybe later", "not sure yet"
  warm       - engaged but noncommittal, general conversation
  cold       - short/vague reply, hard to read
  stop       - says STOP, unsubscribe, remove me, or similar opt-out
  dead       - wrong number, already sold, not interested (firm), or hostile

Reply with only the single word. No punctuation, no explanation."""


def classify_intent(message: str, history: list[dict]) -> str:
    context = "\n".join(
        f"{m['role'].upper()}: {m['text']}" for m in history[-4:]
    )
    user_content = f"Recent conversation:\n{context}\n\nLatest message from contact: {message}"

    try:
        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=_INTENT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        intent = resp.content[0].text.strip().lower()
        return intent if intent in INTENT_TAGS else "warm"
    except Exception as exc:
        log.error("Intent classification failed: %s", exc)
        return "warm"


# ── Reply generation ───────────────────────────────────────────────────────────

def generate_reply(
    persona_name: str,
    contact: dict,
    history: list[dict],
    message: str,
    intent: str,
) -> str:
    persona = get_persona(persona_name)
    first_name = (
        contact.get("firstName") or contact.get("first_name") or ""
    ).strip() or "there"
    address = (contact.get("address1") or "").strip() or "your property"
    pipeline_stage = contact.get("pipelineStageId", "")
    tags = contact.get("tags", [])

    history_text = "\n".join(
        f"{m['role'].upper()}: {m['text']}" for m in history
    ) or "(no prior messages)"

    intent_hint = {
        "hot":         "They're ready to move. Offer to call them in the next 10 minutes.",
        "appointment": "They want to schedule. Confirm eagerness and suggest calling them now or a specific time.",
        "question":    "Answer their question clearly and briefly, then pivot with a qualifying question.",
        "objection":   "Acknowledge their hesitation with empathy. Use NEPQ to uncover what's holding them back.",
        "warm":        "Keep the conversation moving. Ask one qualifying question.",
        "cold":        "Re-engage warmly. Ask one open question about their situation.",
        "dead":        "Respond gracefully and wish them well. One sentence max.",
        "stop":        "Send the opt-out confirmation message only.",
    }.get(intent, "Keep the conversation moving with one qualifying question.")

    user_prompt = f"""Contact name: {first_name}
Property address: {address}
Pipeline stage ID: {pipeline_stage or "unknown"}
Contact tags: {", ".join(tags) if tags else "none"}

Conversation history:
{history_text}

Latest message from {first_name}: {message}

Detected intent: {intent}
Instruction: {intent_hint}

Write your SMS reply as {persona['name']} now."""

    try:
        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=persona["system_prompt"],
            messages=[{"role": "user", "content": user_prompt}],
        )
        reply = resp.content[0].text.strip()
        # Hard cap at 320 chars (2 SMS segments)
        if len(reply) > 320:
            reply = reply[:317] + "..."
        return reply
    except Exception as exc:
        log.error("Reply generation failed: %s", exc)
        return f"Hey {first_name}, this is {persona['name']} from Helpful Home Buyers USA. Just checking in — is now a good time to chat?"


# ── Send SMS ───────────────────────────────────────────────────────────────────

def send_sms(contact_id: str, message: str, from_number: str) -> bool:
    payload: dict = {
        "type":      "SMS",
        "contactId": contact_id,
        "message":   message,
    }
    if from_number:
        payload["fromNumber"] = from_number

    r = _ghl_post("/conversations/messages", json=payload)
    success = r is not None
    if success:
        log.info("AI SMS sent contact=%s from=%s", contact_id, from_number)
    else:
        log.error("AI SMS failed contact=%s", contact_id)
    return success


# ── Tag contact ────────────────────────────────────────────────────────────────

def apply_tags(contact_id: str, intent: str) -> None:
    tags = INTENT_TAGS.get(intent, [])
    if not tags:
        return
    r = _ghl_post(f"/contacts/{contact_id}/tags", json={"tags": tags})
    if r:
        log.info("Tags applied contact=%s intent=%s tags=%s", contact_id, intent, tags)
    else:
        log.error("Tag apply failed contact=%s", contact_id)


# ── Retell outbound call trigger ───────────────────────────────────────────────

def trigger_retell_call(contact: dict, persona_name: str) -> bool:
    """Fire a Retell outbound call to the contact using the matching agent."""
    agent_id = PERSONA_RETELL_AGENT.get(persona_name)
    from_number = PERSONA_PHONE.get(persona_name)

    to_number = (
        contact.get("phone")
        or contact.get("mobilePhone")
        or contact.get("phoneRaw")
        or ""
    ).strip()

    if not agent_id or not from_number or not to_number:
        log.warning(
            "Cannot trigger Retell call: agent_id=%s from=%s to=%s",
            agent_id, from_number, to_number,
        )
        return False

    first_name = (contact.get("firstName") or "").strip()
    address = (contact.get("address1") or "").strip()

    payload = {
        "from_number":  from_number,
        "to_number":    to_number,
        "override_agent_id": agent_id,
        "retell_llm_dynamic_variables": {
            "contact_name":    first_name,
            "property_address": address,
            "contact_id":      contact.get("id", ""),
        },
    }

    try:
        r = requests.post(
            f"{RETELL_BASE}/v2/create-phone-call",
            headers=RETELL_HEADERS,
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            log.info("Retell call triggered contact=%s agent=%s", contact.get("id"), agent_id)
            return True
        log.error("Retell call failed: %s %s", r.status_code, r.text[:200])
        return False
    except Exception as exc:
        log.error("Retell call exception: %s", exc)
        return False


# ── Main entry point ───────────────────────────────────────────────────────────

def handle_sms_ai(
    contact_id: str,
    message: str,
    from_number: str = "",
) -> dict:
    """
    Full AI SMS handler. Returns a result dict for the webhook response.

    Args:
        contact_id:   GHL contact ID (from workflow body)
        message:      Inbound SMS text
        from_number:  GHL phone number the message came in on (used for persona selection)
    """
    log.info("AI SMS handler contact=%s from=%s msg=%.60s", contact_id, from_number, message)

    # ── Stop/opt-out fast path ─────────────────────────────────────────────────
    normalized = message.strip().lower()
    if any(kw in normalized for kw in STOP_KEYWORDS):
        send_sms(contact_id, "You've been removed. Take care.", from_number)
        apply_tags(contact_id, "stop")
        log.info("Opt-out handled contact=%s", contact_id)
        return {"handled": True, "intent": "stop", "call_triggered": False}

    # ── Fetch context ──────────────────────────────────────────────────────────
    contact = fetch_contact(contact_id)
    if not contact:
        log.error("Contact not found: %s", contact_id)
        return {"handled": False, "reason": "contact not found"}

    history = fetch_conversation_history(contact_id)
    persona_name = persona_from_phone(from_number)

    # ── Classify → generate → send ────────────────────────────────────────────
    intent = classify_intent(message, history)
    log.info("Intent: %s contact=%s persona=%s", intent, contact_id, persona_name)

    reply = generate_reply(persona_name, contact, history, message, intent)
    sent = send_sms(contact_id, reply, from_number)

    # ── Side effects ───────────────────────────────────────────────────────────
    apply_tags(contact_id, intent)

    call_triggered = False
    if intent == "hot":
        call_triggered = trigger_retell_call(contact, persona_name)

    return {
        "handled":       True,
        "intent":        intent,
        "persona":       persona_name,
        "sms_sent":      sent,
        "call_triggered": call_triggered,
    }
