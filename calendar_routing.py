import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


log = logging.getLogger(__name__)

DEFAULT_JEFFREY_CALENDAR_ID = "2xJXutj4eTskFPYx8AeL"
DEFAULT_LOCATION_ID = "Jy8irfJWPVtq3vycsvx4"
DEFAULT_FIXED_OWNER_KEY = "jeffrey_bord"
DEFAULT_FIXED_OWNER_NAME = "Jeffrey Bord"


@dataclass(frozen=True)
class CalendarRoute:
    mode: str
    owner_key: str
    owner_name: str
    calendar_id: str
    location_id: str
    selection_reason: str


@dataclass(frozen=True)
class CalendarRep:
    owner_key: str
    owner_name: str
    calendar_id: str
    location_id: str
    active: bool = True


def _normalize_mode(value: Optional[str]) -> str:
    mode = (value or "").strip().lower()
    if mode in {"fixed_owner", "round_robin"}:
        return mode
    return "fixed_owner"


def _fixed_owner_rep() -> CalendarRep:
    return CalendarRep(
        owner_key=os.getenv("CALENDAR_FIXED_OWNER_KEY", DEFAULT_FIXED_OWNER_KEY),
        owner_name=os.getenv("CALENDAR_FIXED_OWNER_NAME", DEFAULT_FIXED_OWNER_NAME),
        calendar_id=os.getenv("CALENDAR_FIXED_OWNER_ID")
        or os.getenv("CALENDAR_ID")
        or DEFAULT_JEFFREY_CALENDAR_ID,
        location_id=os.getenv("CALENDAR_FIXED_OWNER_LOCATION_ID")
        or os.getenv("GHL_LOCATION_ID")
        or DEFAULT_LOCATION_ID,
        active=True,
    )


def _parse_rep_config(raw: str) -> list[CalendarRep]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Invalid CALENDAR_REP_CONFIG_JSON: %s", exc)
        return []

    if not isinstance(payload, list):
        log.warning("CALENDAR_REP_CONFIG_JSON must be a list")
        return []

    reps: list[CalendarRep] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            log.warning("Skipping calendar rep config index %d: expected object", idx)
            continue
        calendar_id = (item.get("calendar_id") or "").strip()
        if not calendar_id:
            log.warning("Skipping calendar rep config index %d: missing calendar_id", idx)
            continue
        reps.append(
            CalendarRep(
                owner_key=(item.get("owner_key") or f"rep_{idx + 1}").strip(),
                owner_name=(item.get("owner_name") or item.get("name") or f"Rep {idx + 1}").strip(),
                calendar_id=calendar_id,
                location_id=(item.get("location_id") or os.getenv("GHL_LOCATION_ID") or DEFAULT_LOCATION_ID).strip(),
                active=bool(item.get("active", True)),
            )
        )
    return reps


def _rep_pool() -> list[CalendarRep]:
    raw = (os.getenv("CALENDAR_REP_CONFIG_JSON") or "").strip()
    if not raw:
        return []
    return [rep for rep in _parse_rep_config(raw) if rep.active]


def _all_known_reps() -> list[CalendarRep]:
    fixed = _fixed_owner_rep()
    reps = [fixed]
    for rep in _rep_pool():
        if rep.calendar_id == fixed.calendar_id and rep.owner_key == fixed.owner_key:
            continue
        reps.append(rep)
    return reps


def _rep_to_route(rep: CalendarRep, *, mode: str, reason: str) -> CalendarRoute:
    return CalendarRoute(
        mode=mode,
        owner_key=rep.owner_key,
        owner_name=rep.owner_name,
        calendar_id=rep.calendar_id,
        location_id=rep.location_id,
        selection_reason=reason,
    )


def _select_round_robin_rep(reps: list[CalendarRep], contact_id: Optional[str]) -> CalendarRep:
    if len(reps) == 1:
        return reps[0]

    if contact_id:
        digest = hashlib.sha256(contact_id.encode("utf-8")).hexdigest()
        index = int(digest[:8], 16) % len(reps)
        return reps[index]

    day_seed = datetime.now(timezone.utc).timetuple().tm_yday
    return reps[day_seed % len(reps)]


def resolve_calendar_route(
    *,
    agent_name: str,
    contact_id: Optional[str] = None,
    requested_calendar_id: Optional[str] = None,
    requested_owner_key: Optional[str] = None,
    mode_override: Optional[str] = None,
) -> CalendarRoute:
    known_reps = _all_known_reps()
    mode = _normalize_mode(mode_override or os.getenv("CALENDAR_ROUTING_MODE"))

    if requested_calendar_id:
        for rep in known_reps:
            if rep.calendar_id == requested_calendar_id:
                return _rep_to_route(rep, mode=mode, reason="requested_calendar_id")
        raise ValueError(f"Unknown calendar_id '{requested_calendar_id}'")

    if requested_owner_key:
        for rep in known_reps:
            if rep.owner_key == requested_owner_key:
                return _rep_to_route(rep, mode=mode, reason="requested_owner_key")
        raise ValueError(f"Unknown routing_owner_key '{requested_owner_key}'")

    if mode == "round_robin":
        reps = _rep_pool()
        if reps:
            chosen = _select_round_robin_rep(reps, contact_id)
            return _rep_to_route(chosen, mode=mode, reason="round_robin_pool")

    fixed_owner = _fixed_owner_rep()
    return _rep_to_route(fixed_owner, mode="fixed_owner", reason="fixed_owner_default")


def routing_summary() -> dict:
    fixed = _fixed_owner_rep()
    reps = _rep_pool()
    return {
        "mode": _normalize_mode(os.getenv("CALENDAR_ROUTING_MODE")),
        "fixed_owner": asdict(fixed),
        "round_robin_rep_count": len(reps),
        "round_robin_reps": [asdict(rep) for rep in reps],
    }
