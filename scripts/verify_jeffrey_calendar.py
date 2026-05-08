#!/usr/bin/env python3
import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = [
    ROOT / ".env",
    ROOT.parent / "ghl-power-dialer-live" / ".env",
]


def load_env() -> None:
    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def post_json(url: str, payload: dict) -> dict:
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def find_slot(base_url: str, timezone: str, days: int) -> tuple[str, dict] | tuple[None, None]:
    for offset in range(days):
        target_date = (date.today() + timedelta(days=offset)).isoformat()
        body = {"date": target_date, "timezone": timezone}
        data = post_json(f"{base_url}/shelby-check-calendar", body)
        slots = data.get("available_slots") or []
        if slots:
            return target_date, slots[0]
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify HHB appointment routing via the live webhook.")
    parser.add_argument("--base-url", default=os.getenv("WEBHOOK_BASE_URL", "https://helpful-homebuyers-webhooks.onrender.com"))
    parser.add_argument("--agent", default="shelby")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--contact-id", default="GdgEEDPemEuCKuiYxAze")
    parser.add_argument("--routing-mode")
    parser.add_argument("--routing-owner-key")
    parser.add_argument("--calendar-id")
    parser.add_argument("--book", action="store_true")
    args = parser.parse_args()

    load_env()

    def _find_slot() -> tuple[str, dict] | tuple[None, None]:
        for offset in range(args.days):
            target_date = (date.today() + timedelta(days=offset)).isoformat()
            body = {
                "date": target_date,
                "timezone": args.timezone,
                "contact_id": args.contact_id,
            }
            if args.routing_mode:
                body["routing_mode"] = args.routing_mode
            if args.routing_owner_key:
                body["routing_owner_key"] = args.routing_owner_key
            if args.calendar_id:
                body["calendar_id"] = args.calendar_id
            data = post_json(f"{args.base_url}/{args.agent}-check-calendar", body)
            slots = data.get("available_slots") or []
            if slots:
                return target_date, {"slot": slots[0], "route": data}
        return None, None

    target_date, slot_payload = _find_slot()
    slot = slot_payload["slot"] if slot_payload else None
    route = slot_payload["route"] if slot_payload else None
    if not slot:
        print(json.dumps({"ok": False, "reason": "no_open_slots_found", "days_scanned": args.days}, indent=2))
        return 1

    result = {
        "ok": True,
        "agent": args.agent,
        "date": target_date,
        "slot": slot,
        "route": {
            "calendar_id": route.get("calendar_id"),
            "calendar_owner_key": route.get("calendar_owner_key"),
            "calendar_owner_name": route.get("calendar_owner_name"),
            "routing_mode": route.get("routing_mode"),
            "routing_reason": route.get("routing_reason"),
        },
    }

    if args.book:
        booking_payload = {
            "contact_id": args.contact_id,
            "start_time": slot["datetime_iso"],
            "title": "Helpful Home Buyers USA Calendar Proof",
            "notes": "Automated verification booking for the HHB appointment routing layer.",
        }
        if route and route.get("calendar_id"):
            booking_payload["calendar_id"] = route["calendar_id"]
        if route and route.get("calendar_owner_key"):
            booking_payload["routing_owner_key"] = route["calendar_owner_key"]
        if args.routing_mode:
            booking_payload["routing_mode"] = args.routing_mode
        booking = post_json(
            f"{args.base_url}/{args.agent}-book-appointment",
            booking_payload,
        )
        result["booking"] = booking

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
