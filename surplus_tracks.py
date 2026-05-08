"""
surplus_tracks.py — CR_CashRights Surplus Funds System

Handles Reagan Retell call outcomes for the surplus funds pipeline.
Maps call outcomes to GHL stage moves on pipeline pPzXtUk7LwCzNfXuD1v1.
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
    "Version":       "2021-07-28",
    "Content-Type":  "application/json",
}
GHL_LOCATION_ID      = os.getenv("GHL_LOCATION_ID", "Jy8irfJWPVtq3vycsvx4")
REAGAN_PHONE         = "+17078463387"
REAGAN_AGENT_ID      = "agent_5c5a513db86a21993f8c148ac6"
SURPLUS_PIPELINE_ID  = "pPzXtUk7LwCzNfXuD1v1"

# Stage names exactly as entered in GHL
STAGE_HOT            = "Reagan Calling — HOT"
STAGE_AGREEMENT_SENT = "$100 Sent — Agreement Out"
STAGE_NOT_INTERESTED = "Not Interested — Nurture"
STAGE_DNC            = "Dead / DNC"

TAG_AGREEMENT_SIGNED = "agreement-signed"
TAG_HOT              = "track-1-hot"

_surplus_stage_cache: dict = {}

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


# ── Stage cache ───────────────────────────────────────────────────────────────

def _load_stage_cache() -> None:
    """Populate _surplus_stage_cache from GHL pipeline stages."""
    r = _ghl_get("/opportunities/pipelines", params={"locationId": GHL_LOCATION_ID})
    if not r or r.status_code != 200:
        log.error("Could not fetch GHL pipelines to build surplus stage cache")
        return
    for pipeline in r.json().get("pipelines", []):
        if pipeline.get("id") == SURPLUS_PIPELINE_ID:
            for stage in pipeline.get("stages", []):
                _surplus_stage_cache[stage["name"].lower()] = stage["id"]
            log.info("Surplus stage cache loaded: %d stages", len(_surplus_stage_cache))
            return
    log.error("Surplus pipeline %s not found in GHL", SURPLUS_PIPELINE_ID)


def _stage_id(stage_name: str) -> Optional[str]:
    if not _surplus_stage_cache:
        _load_stage_cache()
    sid = _surplus_stage_cache.get(stage_name.lower())
    if not sid:
        log.error("Stage '%s' not found in surplus pipeline cache", stage_name)
    return sid


# ── GHL helpers ───────────────────────────────────────────────────────────────

def _add_note(contact_id: str, body: str) -> None:
    r = _ghl_post(f"/contacts/{contact_id}/notes", json={"body": body, "userId": ""})
    if not (r and r.status_code in (200, 201)):
        log.error("Note failed for contact %s: %s", contact_id, r.text[:200] if r else "no response")


def _apply_tags(contact_id: str, tags: list[str]) -> bool:
    r = _ghl_post(f"/contacts/{contact_id}/tags", json={"tags": tags})
    return r is not None and r.status_code in (200, 201)


def _add_task(contact_id: str, title: str, due_offset_days: int = 1) -> None:
    """Add a follow-up task to a contact."""
    from datetime import datetime, timezone, timedelta
    due = datetime.now(timezone.utc) + timedelta(days=due_offset_days)
    r = _ghl_post(
        f"/contacts/{contact_id}/tasks",
        json={
            "title":   title,
            "dueDate": due.isoformat(),
            "status":  "incomplete",
        },
    )
    if not (r and r.status_code in (200, 201)):
        log.warning("Task creation failed for contact %s: %s", contact_id, r.text[:200] if r else "no response")


def _find_surplus_opp(contact_id: str) -> Optional[str]:
    """Find the most recent open surplus opportunity for a contact."""
    r = _ghl_get(
        "/opportunities/search",
        params={
            "location_id": GHL_LOCATION_ID,
            "contact_id":  contact_id,
            "pipeline_id": SURPLUS_PIPELINE_ID,
        },
    )
    if not r or r.status_code != 200:
        return None
    opps = r.json().get("opportunities", [])
    return opps[0]["id"] if opps else None


def _move_opp(opp_id: str, stage_name: str) -> bool:
    """Move a surplus opportunity to the named stage."""
    sid = _stage_id(stage_name)
    if not sid:
        return False
    r = _ghl_put(f"/opportunities/{opp_id}", json={"pipelineStageId": sid})
    ok = r is not None and r.status_code in (200, 201)
    if not ok:
        log.error("Stage move to '%s' failed for opp %s: %s", stage_name, opp_id, r.text[:200] if r else "no response")
    return ok


# ── Note builder ──────────────────────────────────────────────────────────────

def _build_note(outcome: str, summary: str, transcript: str, call_id: str, duration_ms: int) -> str:
    lines = [
        "📞 Reagan — Surplus Funds Call",
        f"Call ID: {call_id}",
        f"Outcome: {outcome}",
        f"Duration: {round(duration_ms / 1000)}s",
    ]
    if summary:
        lines.append(f"\nAI Summary: {summary}")
    if transcript:
        lines.append(f"\n--- FULL TRANSCRIPT ---\n{transcript}")
    return "\n".join(lines)


# ── Outcome handler ───────────────────────────────────────────────────────────

def handle_call_outcome(payload: dict) -> dict:
    """
    Handle a Retell call-ended webhook for Reagan (surplus funds).

    Expected outcomes in call_analysis.custom_analysis_data.call_outcome:
      - interested          → stay in HOT stage + add note
      - agreement_sent      → move to "$100 Sent — Agreement Out"
      - agreement_signed    → move to "$100 Sent — Agreement Out" + tag agreement-signed
      - not_interested      → move to "Not Interested — Nurture"
      - no_answer           → stay in current stage + add reschedule task
      - dnc                 → move to "Dead / DNC"

    A GHL note with the call summary is added after every call.
    """
    call_id       = payload.get("call_id", "")
    call_analysis = payload.get("call_analysis") or {}
    custom        = call_analysis.get("custom_analysis_data") or {}
    dynamic_vars  = payload.get("retell_llm_dynamic_variables") or {}

    contact_id   = dynamic_vars.get("contact_id") or custom.get("contact_id", "")
    call_outcome = (custom.get("call_outcome") or "no_answer").strip().lower()
    summary      = call_analysis.get("call_summary", "")
    transcript   = payload.get("transcript", "")
    duration_ms  = payload.get("duration_ms", 0)

    log.info(
        "surplus call-outcome call_id=%s contact=%s outcome=%s duration=%ds",
        call_id, contact_id, call_outcome, round(duration_ms / 1000),
    )

    if not contact_id:
        log.warning("surplus_tracks: no contact_id for call %s", call_id)
        return {"success": False, "error": "no contact_id"}

    note = _build_note(call_outcome, summary, transcript, call_id, duration_ms)
    _add_note(contact_id, note)

    opp_id = _find_surplus_opp(contact_id)
    stage_moved = False
    tag_applied = False
    task_added  = False

    if call_outcome == "interested":
        # Stay in HOT — note is enough
        log.info("surplus: interested — contact %s stays HOT", contact_id)

    elif call_outcome in ("agreement_sent", "agreement_signed"):
        if opp_id:
            stage_moved = _move_opp(opp_id, STAGE_AGREEMENT_SENT)
        if call_outcome == "agreement_signed":
            tag_applied = _apply_tags(contact_id, [TAG_AGREEMENT_SIGNED])

    elif call_outcome == "not_interested":
        if opp_id:
            stage_moved = _move_opp(opp_id, STAGE_NOT_INTERESTED)

    elif call_outcome == "no_answer":
        # Stay in current stage; add a reschedule task
        _add_task(contact_id, "☎️ Reagan — reschedule surplus call (no answer)", due_offset_days=1)
        task_added = True
        log.info("surplus: no_answer — reschedule task added for contact %s", contact_id)

    elif call_outcome == "dnc":
        if opp_id:
            stage_moved = _move_opp(opp_id, STAGE_DNC)

    else:
        log.warning("surplus_tracks: unknown outcome '%s' for contact %s", call_outcome, contact_id)

    return {
        "success":      True,
        "contact_id":   contact_id,
        "outcome":      call_outcome,
        "opp_id":       opp_id,
        "stage_moved":  stage_moved,
        "tag_applied":  tag_applied,
        "task_added":   task_added,
        "note_added":   True,
    }
